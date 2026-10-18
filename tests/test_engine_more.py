import os, sys, time
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from harness import *
import soundfile as sf

D = "/tmp/claude-0/-home-claude/634049f8-76f1-5b8b-ba6e-ce947dd8eb75/scratchpad/media/"
R = []
def check(name, ok, extra=""):
    print(("PASS " if ok else "FAIL ") + name, extra); R.append(ok)

def run_track(H, track, timeout=20, wait_finish=True):
    m = H.mark()
    H.cmd("load", track=track)
    if wait_finish:
        H.wait("finished", timeout=timeout, since=m)
        time.sleep(0.2)

def main():
    exp1 = make_noise_flac(D + "n1.flac", 1.5, 44100, 16, 2, seed=1)
    exp24 = make_noise_flac(D + "n24.flac", 1.2, 96000, 24, 2, seed=3)
    # --- CUE範囲 ---
    H = Harness("capture-paced", "S32_LE", settings=dict(ram_playback=False, idle_close_s=0))
    try:
        a, b = 10000, 30000
        run_track(H, {"id": 1, "path": D + "n1.flac", "start_frame": a, "end_frame": b})
        x = left_i32_from_stream(H.captured(), "S32_LE", 2); s0 = find_start(x)
        check("cue range exact", np.array_equal(x[s0:s0 + (b - a)], exp1[a:b]) and not np.any(x[s0 + (b - a):s0 + (b - a) + 2000]), f"s0={s0}")
        ev = [e for e in H.events if e["ev"] == "track_changed"][0]
        check("cue duration", abs(ev["track"].get("end_frame", 0) - b) == 0)
    finally:
        H.close()
    # --- 音量 ---
    H = Harness("capture-paced", "S32_LE", settings=dict(ram_playback=False, idle_close_s=0, volume=0.5))
    try:
        H.cmd("volume", v=0.5)
        time.sleep(0.2)
        run_track(H, {"id": 1, "path": D + "n1.flac"})
        x = left_i32_from_stream(H.captured(), "S32_LE", 2); s0 = find_start(x)
        got = x[s0:s0 + len(exp1)].astype(np.float64); want = exp1.astype(np.float64) * 0.5
        err = np.abs(got - want).max() / 2**8   # 24bit LSB 単位
        check("volume 0.5 within 2 LSB(24bit)", err <= 2.0, f"maxerr={err:.2f} LSB")
        sig = [e for e in H.events if e["ev"] == "track_changed"][0]["signal"]
        check("signal shows soft volume + quantize", any("ソフト音量" in s for s in sig["steps"]), str(sig["steps"]))
    finally:
        H.close()
    # --- リサンプル (固定192k / family_max / x2) ---
    for mode, cap, expect_rate in [("fixed", 192000, 192000), ("family_max", 192000, 176400), ("x2", 192000, 88200)]:
        H = Harness("capture-paced", "S32_LE", settings=dict(ram_playback=False, idle_close_s=0, resample_enabled=True,
                    resample_mode=mode, fixed_rate=192000, rate_cap=cap))
        try:
            # 440Hz 正弦 6s の a16.flac を 1.5s だけ使う
            run_track(H, {"id": 1, "path": D + "a16.flac", "start_frame": 0, "end_frame": int(44100 * 1.5)}, timeout=25)
            sess = H.eng._test_backend_obj.sessions[-1]
            x = left_i32_from_stream(H.captured(), "S32_LE", 2); s0 = find_start(x)
            y = x[s0:, 0].astype(np.float64) / 2**31
            n = int(1.4 * sess.rate)
            seg = y[sess.rate // 4: sess.rate // 4 + n]
            spec = np.abs(np.fft.rfft(seg * np.hanning(len(seg))))
            f = np.argmax(spec) * sess.rate / len(seg)
            dur = len(np.trim_zeros(y, 'b')) / sess.rate
            check(f"resample {mode}: rate={sess.rate}", sess.rate == expect_rate and abs(f - 440) < 2 and abs(dur - 1.5) < 0.02,
                  f"peak={f:.1f}Hz dur={dur:.3f}s")
        finally:
            H.close()
    # --- ビット深度/強制ダウンサンプル (cap 96k で 96k 24bit は素通し、192k cap=48k なら強制) ---
    H = Harness("capture-paced", "S24_3LE", settings=dict(ram_playback=False, idle_close_s=0, rate_cap=48000))
    try:
        run_track(H, {"id": 1, "path": D + "n24.flac"}, timeout=25)
        sess = H.eng._test_backend_obj.sessions[-1]
        sig = [e for e in H.events if e["ev"] == "track_changed"][0]["signal"]
        check("forced downsample 96k->48k", sess.rate == 48000 and any("強制" in n for n in sig["notes"]), str(sig["notes"]))
    finally:
        H.close()
    # --- 非ギャップレス(レート変更)遷移 ---
    H = Harness("capture-paced", "S32_LE", settings=dict(ram_playback=False, idle_close_s=0, prefetch=True))
    try:
        m = H.mark()
        H.cmd("set_next", track={"id": 2, "path": D + "n24.flac"})
        H.cmd("load", track={"id": 1, "path": D + "n1.flac"})
        H.wait("finished", timeout=25, since=m); time.sleep(0.3)
        sessions = H.eng._test_backend_obj.sessions
        check("rate change reopens device", [s.rate for s in sessions] == [44100, 96000], str([s.rate for s in sessions]))
        tc = [e["id"] for e in H.events if e["ev"] == "track_changed"]
        check("both tracks announced", tc == [1, 2], str(tc))
    finally:
        H.close()
    # --- 一時停止と再開: データが失われない ---
    H = Harness("capture-paced", "S32_LE", settings=dict(ram_playback=False, idle_close_s=0))
    try:
        m = H.mark()
        H.cmd("load", track={"id": 1, "path": D + "n1.flac"})
        time.sleep(0.8)
        H.cmd("pause"); time.sleep(0.6)
        st = [e for e in H.events if e["ev"] == "status"][-1]
        p1 = st["pos"]; time.sleep(0.3)
        st2 = [e for e in H.events if e["ev"] == "status"][-1]
        check("position frozen while paused", abs(st2["pos"] - p1) < 0.02 and st2["state"] == "paused", f"{p1} {st2['pos']}")
        H.cmd("resume")
        H.wait("finished", timeout=15, since=m); time.sleep(0.2)
        x = left_i32_from_stream(H.captured(), "S32_LE", 2)
        nz = np.nonzero(np.any(x != 0, axis=1))[0]
        got = x[nz]            # 無音を除いた音声部分だけを連結
        check("pause/resume lossless", np.array_equal(got[:len(exp1)], exp1) and len(got) == len(exp1), f"{len(got)} vs {len(exp1)}")
    finally:
        H.close()
    # --- シーク (RAM局所 / ストリーミング新規) ---
    for ram in (True, False):
        H = Harness("capture-paced", "S32_LE", settings=dict(ram_playback=ram, idle_close_s=0))
        try:
            m = H.mark()
            H.cmd("load", track={"id": 1, "path": D + "n1.flac"})
            time.sleep(0.5)
            H.cmd("seek", pos=1.0)
            H.wait("finished", timeout=15, since=m); time.sleep(0.2)
            x = left_i32_from_stream(H.captured(), "S32_LE", 2)
            tail = exp1[44100:]
            # 出力の中に「1.0秒地点以降」がそのまま連続して含まれているか
            idx = np.nonzero(np.all(x == tail[0], axis=1))[0]
            ok = any(i + len(tail) <= len(x) and np.array_equal(x[i:i + len(tail)], tail) for i in idx)
            check(f"seek ram={ram}: tail from 1.0s continuous", ok)
        finally:
            H.close()
    # --- m4a(AAC)/ALAC/mp3 ---
    for name in ["t.m4a", "t_alac.m4a", "t.mp3"]:
        H = Harness("capture-paced", "S32_LE", settings=dict(ram_playback=False, idle_close_s=0))
        try:
            run_track(H, {"id": 1, "path": D + name}, timeout=25)
            sess = H.eng._test_backend_obj.sessions[-1]
            x = left_i32_from_stream(H.captured(), "S32_LE", sess.channels); s0 = find_start(x)
            y = x[s0:, 0].astype(np.float64) / 2**31
            seg = y[sess.rate // 2: sess.rate // 2 + sess.rate]
            spec = np.abs(np.fft.rfft(seg * np.hanning(len(seg))))
            f = np.argmax(spec) * sess.rate / len(seg)
            dur = len(np.trim_zeros(y, 'b')) / sess.rate
            check(f"decode {name}: 330Hz, dur", abs(f - 330) < 3 and abs(dur - 4.0) < 0.15, f"f={f:.1f} dur={dur:.2f} ch={sess.channels}")
        finally:
            H.close()
    print("ALL PASS" if all(R) else "SOME FAIL")

if __name__ == "__main__":
    main()
