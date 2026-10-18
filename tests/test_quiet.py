"""静音モード: 再生中の状態通知・メトリクスが間引かれ、準備プロセスの待機が長くなる。"""
import os, sys, time, textwrap
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from harness import Harness

GEN = textwrap.dedent('''
import sys, time, numpy as np
rate=48000; chunk=480
t0=time.monotonic(); n=0
out=sys.stdout.buffer
while True:
    k=np.arange(n,n+chunk)
    v=(np.sin(2*np.pi*440*k/rate)*0.5*2**31).astype('<i4')
    out.write(np.stack([v,v],1).tobytes()); out.flush()
    n+=chunk
    d=t0+n/rate-time.monotonic()
    if d>0: time.sleep(d)
''')


def run(quiet):
    H = Harness("capture-paced", "S32_LE", settings=dict(s32_valid_bits=32, capture_rate=48000, idle_close_s=0, quiet_mode=quiet))
    try:
        H.eng._apply_quiet()
        m = H.mark()
        H.cmd("capture_start", sink_input=1, label="t")
        H.wait("capture_started", timeout=10, since=m)
        time.sleep(0.5)
        m = H.mark()
        time.sleep(5.0)
        ev = H.events[m:]
        return sum(1 for e in ev if e["ev"] == "status"), sum(1 for e in ev if e["ev"] == "metrics")
    finally:
        H.close()


def main():
    open("/tmp/gen_cap.py", "w").write(GEN)
    os.environ["MIYAMIZU_CAPTURE_CMD"] = f"{sys.executable} /tmp/gen_cap.py"
    fails = 0
    def check(name, ok, extra=""):
        nonlocal fails
        print(("PASS " if ok else "FAIL ") + name, extra); fails += (not ok)
    ns, nm = run(False)
    qs, qm = run(True)
    print("通常 status/metrics =", ns, nm, " 静音 =", qs, qm)
    check("通常は 0.1 秒ごとの状態通知", ns >= 40, ns)
    check("静音モードは状態通知が大幅に減る", qs <= 8, qs)
    check("静音モードはメトリクスが間引かれる", qm <= 2 and nm >= 4, f"{nm} -> {qm}")
    print("ALL PASS" if not fails else f"{fails} FAIL")
    sys.exit(1 if fails else 0)


if __name__ == "__main__":
    main()
