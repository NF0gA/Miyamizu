"""事前変換(先行)の確認: 先の曲が RAM に載り、ギャップレス/ビット一致で再生され、事前変換した RAM がそのまま使われる。
リサンプル有りでも動くこと(出力フレーム数が一致)と、alsa 系の lead-in 無音も確認する。"""
import os, sys, time
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from harness import *

D = "/tmp/hifi_prerender/"
os.makedirs(D, exist_ok=True)


def check(name, ok, extra=""):
    print(("PASS " if ok else "FAIL ") + name, extra)
    return ok


def run(resample: bool):
    names = ["a", "b", "c", "d"]
    exps = {n: make_noise_flac(D + n + ".flac", 1.2, 44100, 16, 2, seed=i + 10) for i, n in enumerate(names)}
    tr = {n: {"id": i + 1, "path": D + n + ".flac"} for i, n in enumerate(names)}
    st = dict(ram_playback=True, prefetch=True, prerender_ahead=2, prerender_total_mb=512, idle_close_s=0)
    if resample:
        st.update(resample_enabled=True, resample_mode="fixed", fixed_rate=96000, resample_quality="HQ")
    H = Harness("capture-paced", "S32_LE", settings=st)
    ok_all = []
    try:
        m = H.mark()
        H.cmd("set_ahead", tracks=[tr["c"], tr["d"]])
        H.cmd("set_next", track=tr["b"])
        H.cmd("load", track=tr["a"])
        seen_ahead = False
        order = ["a", "b", "c", "d"]
        handled = 0
        t0 = time.time()
        while time.time() - t0 < 30:
            with H.lock:
                evs = list(H.events[m:])
            for e in evs[handled:]:
                if e["ev"] == "metrics":
                    for r in e.get("ram", []):
                        if r.get("role") == "ahead" and r.get("done"):
                            seen_ahead = True
                if e["ev"] == "need_next":
                    cur = e["id"] - 1
                    if cur + 1 < len(order):
                        H.cmd("set_next", track=tr[order[cur + 1]])
                        H.cmd("set_ahead", tracks=[tr[n] for n in order[cur + 2: cur + 4]])
            handled = len(evs)
            if any(e["ev"] == "finished" for e in evs):
                break
            time.sleep(0.05)
        time.sleep(0.3)
        ok_all.append(check(f"resample={resample}: 先行(ahead)の曲が RAM に載った(metrics の role=ahead, done)", seen_ahead))
        x = left_i32_from_stream(H.captured(), "S32_LE", 2)
        s0 = find_start(x)
        tc = [e["id"] for e in H.events if e["ev"] == "track_changed"]
        ok_all.append(check(f"resample={resample}: 再生順 a,b,c,d", tc == [1, 2, 3, 4], str(tc)))
        if not resample:
            want = np.concatenate([exps[n] for n in names])
            got = x[s0:s0 + len(want)]
            ok_all.append(check("ビット一致・ギャップレス(先行変換した曲を含む)", np.array_equal(got, want), f"{len(got)}/{len(want)}"))
        else:
            nz = np.nonzero(np.any(x != 0, axis=1))[0]
            exp_len = int(4 * 1.2 * 96000)
            ok_all.append(check("リサンプル有り: 出力フレーム数がほぼ一致", abs((nz[-1] - nz[0] + 1) - exp_len) < 2000, f"{nz[-1]-nz[0]+1} vs {exp_len}"))
        ok_all.append(check("アンダーランなし", H.eng.out.underruns == 0, str(H.eng.out.underruns)))
        ok_all.append(check("先行ソースは使い切られて残らない", len(H.eng._ahead) == 0, str(list(H.eng._ahead))))
    finally:
        H.close()
    return ok_all


def main():
    res = run(False) + run(True)
    print("ALL PASS" if all(res) else "SOME FAIL")


if __name__ == "__main__":
    main()
