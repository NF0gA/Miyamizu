import os, sys, time
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from harness import *

D = "/tmp/claude-0/-home-claude/634049f8-76f1-5b8b-ba6e-ce947dd8eb75/scratchpad/media/"
os.makedirs(D, exist_ok=True)
exp1 = make_noise_flac(D + "n1.flac", 1.5, 44100, 16, 2, seed=1)
exp2 = make_noise_flac(D + "n2.flac", 1.2, 44100, 16, 2, seed=2)
exp24 = make_noise_flac(D + "n24.flac", 1.2, 44100, 24, 2, seed=3)

def check(name, ok, extra=""):
    print(("PASS " if ok else "FAIL ") + name, extra)
    return ok

def main():
    results = []
    for fmt in ["S32_LE", "S24_3LE", "S24_LE", "S16_LE"]:
        for ram in [False, True]:
            H = Harness("capture-paced", fmt, settings=dict(ram_playback=ram, prefetch=True, idle_close_s=0))
            try:
                m = H.mark()
                H.cmd("set_next", track={"id": 2, "path": D + "n2.flac"})
                H.cmd("load", track={"id": 1, "path": D + "n1.flac"})
                H.wait("finished", timeout=15, since=m)
                time.sleep(0.2)
                raw = H.captured()
                x = left_i32_from_stream(raw, fmt, 2)
                s0 = find_start(x)
                n1, n2 = len(exp1), len(exp2)
                got = x[s0:s0 + n1 + n2]
                valid = {"S16_LE": 16}.get(fmt, 24)
                want = np.concatenate([exp1, exp2])
                if valid == 16 and fmt == "S16_LE":
                    pass
                ok = np.array_equal(got, want)
                if not ok:
                    bad = np.nonzero(np.any(got != want, axis=1))[0]
                    print("   mismatch", len(bad), bad[:3], "len(got)", len(got))
                results.append(check(f"bitperfect+gapless fmt={fmt} ram={ram}", ok,
                                     f"start={s0} len={len(got)}/{n1+n2} underruns={H.eng.out.underruns}"))
                # status/tracks
                tc = [e["id"] for e in H.events if e["ev"] == "track_changed"]
                results.append(check("  track_changed order", tc == [1, 2], str(tc)))
                sig = [e for e in H.events if e["ev"] == "track_changed"][0]["signal"]
                results.append(check("  signal steps empty (no processing)", sig["steps"] == [], str(sig["steps"])))
            finally:
                H.close()
    print("ALL PASS" if all(results) else "SOME FAIL")

if __name__ == '__main__':
    main()
