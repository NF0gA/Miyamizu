"""デバイスを開き直した直後の無音(lead-in): ALSA/custom 出力でだけ、曲頭の前に指定時間の無音が入る。"""
import os, sys, time
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from harness import *

D = "/tmp/hifi_leadin/"
os.makedirs(D, exist_ok=True)


def run(kind, lead):
    make_noise_flac(D + "a.flac", 0.8, 44100, 16, 2, seed=3)
    H = Harness("capture-paced", "S32_LE", settings=dict(lead_in_ms=lead, idle_close_s=0))
    try:
        H.eng._kind = lambda: kind
        m = H.mark()
        H.cmd("load", track={"id": 1, "path": D + "a.flac"})
        H.wait("finished", timeout=15, since=m)
        time.sleep(0.2)
        x = left_i32_from_stream(H.captured(), "S32_LE", 2)
        return find_start(x) / 44100 * 1000
    finally:
        H.close()


def main():
    res = []
    for kind, lead, lo, hi in [("custom", 600, 590, 700), ("test", 600, 0, 250), ("custom", 0, 0, 250)]:
        ms = run(kind, lead)
        ok = lo <= ms <= hi
        print(("PASS " if ok else "FAIL ") + f"kind={kind} lead_in={lead}ms → 曲頭までの無音(ソース準備待ち約0.14秒を含む) {ms:.0f} ms (期待 {lo}-{hi})")
        res.append(ok)
    print("ALL PASS" if all(res) else "SOME FAIL")


if __name__ == "__main__":
    main()
