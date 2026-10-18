"""取り込みのクロック補正: 連続補正(ratio)は間引き・複製をほとんど使わず、波形も綺麗。slip との比較。"""
import os, sys, time, textwrap
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import numpy as np
from harness import Harness, left_i32_from_stream, find_start

GEN = textwrap.dedent('''
import sys, time, numpy as np
rate=48000; chunk=480; ppm=1.0005
t0=time.monotonic(); n=0
out=sys.stdout.buffer
while True:
    k=np.arange(n,n+chunk)
    v=(np.sin(2*np.pi*1000*k/rate)*0.5*2**31).astype('<i4')
    out.write(np.stack([v,v],1).tobytes()); out.flush()
    n+=chunk
    due=t0+n/rate/ppm
    d=due-time.monotonic()
    if d>0: time.sleep(d)
''')


def run(sync):
    H = Harness("capture-paced", "S32_LE", settings=dict(s32_valid_bits=32, capture_rate=48000, capture_latency_ms=60,
                                                          idle_close_s=0, capture_sync=sync, capture_latency_auto=False))
    try:
        m = H.mark()
        H.cmd("capture_start", sink_input=1, label="t")
        H.wait("capture_started", timeout=10, since=m)
        time.sleep(7.0)
        st = dict(H.eng._cap["stats"]) if H.eng._cap else {}
        H.cmd("capture_stop")
        H.wait("capture_stopped", timeout=10, since=m)
        x = left_i32_from_stream(H.captured(), "S32_LE", 2)
        y = x[find_start(x):, 0].astype(np.float64) / 2**31
        nz = np.nonzero(np.abs(y) > 1e-9)[0]
        return y[:nz[-1] + 1], st
    finally:
        H.close()


def snr(y):
    y = y[4800:-4800]
    w = np.kaiser(len(y), 25)
    mag = np.abs(np.fft.rfft(y * w))
    k = mag.argmax()
    mask = np.ones_like(mag, bool)
    mask[max(0, k - 40):k + 41] = False
    return 20 * np.log10(mag[k] / np.sqrt((mag[mask] ** 2).sum()))


def main():
    open("/tmp/gen_cap.py", "w").write(GEN)
    os.environ["MIYAMIZU_CAPTURE_CMD"] = f"{sys.executable} /tmp/gen_cap.py"
    fails = 0
    def check(name, ok, extra=""):
        nonlocal fails
        print(("PASS " if ok else "FAIL ") + name, extra); fails += (not ok)
    yr, sr = run("ratio")
    ys, ss = run("slip")
    print("ratio stats", sr, "snr", round(snr(yr), 1))
    print("slip  stats", ss, "snr", round(snr(ys), 1))
    check("ratio: 連続補正が働く(ppm が 0 でない)", abs(sr.get("ppm", 0)) > 20, sr.get("ppm"))
    check("ratio: 間引き・複製をほとんど使わない", (sr.get("dropped", 0) + sr.get("duped", 0)) <= 4,
          f"{sr.get('dropped')}+{sr.get('duped')} vs {ss.get('dropped')}+{ss.get('duped')}")
    check("ratio: 波形が綺麗(SN比 80dB 以上)", snr(yr) > 80, f"{snr(yr):.1f}dB")
    check("ratio の方が slip より綺麗", snr(yr) > snr(ys) + 10, f"{snr(yr):.1f} vs {snr(ys):.1f}")
    print("ALL PASS" if not fails else f"{fails} FAIL")
    sys.exit(1 if fails else 0)


if __name__ == "__main__":
    main()
