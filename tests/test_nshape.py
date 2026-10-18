"""ノイズシェーピング: 24bit 音源 → 16bit 出力で、聴感の敏感な帯域(2〜5kHz)のノイズが TPDF のみより下がる。素通し経路は変えない。"""
import os, sys, time, tempfile
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from harness import *
import soundfile as sf

D = tempfile.mkdtemp(prefix="nshape-")


def band(e, a, b, fs=44100):
    w = np.kaiser(len(e), 14)
    S = np.abs(np.fft.rfft(e * w)) ** 2
    f = np.fft.rfftfreq(len(e), 1 / fs)
    return 10 * np.log10(S[(f >= a) & (f < b)].mean() + 1e-30)


def run(shape, path, ref, fmt="S16_LE"):
    H = Harness("capture-paced", fmt, settings=dict(ram_playback=False, idle_close_s=0, noise_shaping=shape, dither=True))
    try:
        m = H.mark()
        H.cmd("load", track={"id": 1, "path": path})
        H.wait("finished", timeout=20, since=m)
        time.sleep(0.2)
        x = left_i32_from_stream(H.captured(), fmt, 2)
        s0 = find_start(x)
        got = x[s0:s0 + len(ref)]
        sig = [e for e in H.events if e["ev"] == "track_changed"][0]["signal"]
        return got, sig
    finally:
        H.close()


def main():
    fs = 44100
    t = np.arange(fs * 3) / fs
    v = (0.3 * np.sin(2 * np.pi * 1000 * t) * (2 ** 23)).astype(np.int32)
    ref = np.stack([v, v], 1) << 8                                  # left-justified 24bit
    path = D + "/s24.flac"
    sf.write(path, np.stack([v, v], 1) << 8, fs, subtype="PCM_24") if False else sf.write(path, np.stack([v, v], 1) / 2 ** 23, fs, subtype="PCM_24")
    fails = 0
    def check(name, ok, extra=""):
        nonlocal fails
        print(("PASS " if ok else "FAIL ") + name, extra); fails += (not ok)
    g0, s0 = run("off", path, ref)
    g1, s1 = run("lipshitz", path, ref)
    e0 = (g0[:, 0].astype(np.float64) - ref[:, 0]) / 2 ** 31
    e1 = (g1[:, 0].astype(np.float64) - ref[:, 0]) / 2 ** 31
    n = min(len(e0), len(e1))
    lo0, lo1 = band(e0[:n], 2000, 5000), band(e1[:n], 2000, 5000)
    hi0, hi1 = band(e0[:n], 18000, 21000), band(e1[:n], 18000, 21000)
    print(f"2-5kHz: off {lo0:.1f} dB / lipshitz {lo1:.1f} dB   18-21kHz: off {hi0:.1f} / lipshitz {hi1:.1f}")
    check("16bit 出力で下位ビットが 0", not np.any(g1 & 0xFFFF) and not np.any(g0 & 0xFFFF))
    check("2〜5kHz のノイズが 10dB 以上下がる", lo1 < lo0 - 10, f"{lo0 - lo1:.1f}dB")
    check("高域のノイズは増える(整形されている)", hi1 > hi0 + 5)
    check("信号パスに表示", any("ノイズシェーピング" in s for s in s1["steps"]) and not any("ノイズシェーピング" in s for s in s0["steps"]), str(s1["steps"]))
    # 素通し(16bit→16bit)は影響を受けない
    x16 = (0.3 * np.cos(2 * np.pi * 1000 * t + 0.3) * 32767).astype(np.int16)
    p16 = D + "/s16.flac"
    sf.write(p16, np.stack([x16, x16], 1), fs, subtype="PCM_16")
    ref16 = np.stack([x16, x16], 1).astype(np.int32) << 16
    g2, s2 = run("lipshitz", p16, ref16)
    check("ビットパーフェクトの素通しは変わらない", np.array_equal(g2, ref16) and s2["steps"] == [], str(s2["steps"]))
    print("ALL PASS" if not fails else f"{fails} FAIL")
    sys.exit(1 if fails else 0)


if __name__ == "__main__":
    main()
