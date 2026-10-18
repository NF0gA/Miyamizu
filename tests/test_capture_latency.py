"""アプリ音声: 開始前に溜まった分を捨てて遅延を持ち越さない/ストリームの音量判定。"""
import os, sys, time, textwrap
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import numpy as np
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


def main():
    open("/tmp/gen_cap2.py", "w").write(GEN)
    os.environ["MIYAMIZU_CAPTURE_CMD"] = f"{sys.executable} /tmp/gen_cap2.py"
    # 再生開始を遅らせる(lead-in 1 秒): 従来は、この間に溜まった分が遅延として残り続けた
    H = Harness("capture-paced", "S32_LE", settings=dict(s32_valid_bits=32, capture_rate=48000, capture_latency_ms=40,
                idle_close_s=0, lead_in_ms=1000))
    fails = 0
    def check(name, ok, extra=""):
        nonlocal fails
        print(("PASS " if ok else "FAIL ") + name, extra); fails += (not ok)
    try:
        eng = H.eng
        eng.out.lead_in_frames = 0
        m = H.mark(); H.cmd("capture_start", sink_input=1, label="t"); H.wait("capture_started", timeout=10, since=m)
        time.sleep(3.0)
        mets = [e for e in H.events[m:] if e["ev"] == "metrics" and e.get("cap_latency_ms") is not None]
        check("遅延が計測できる", len(mets) > 0)
        lat = mets[-1]["cap_latency_ms"] if mets else 9999
        # 目標 40ms + 出力側(10ms×4 = 40ms)程度。以前(目標120ms + 120ms + 持ち越し)より大幅に小さい
        check("遅延が目標付近(持ち越しなし)", lat < 150, f"{lat} ms")
        p = eng.out.params
        check("取り込み中は短い出力周期", p is not None and abs(p.period_ms - eng.settings.capture_period_ms) < 1e-6 and p.periods == eng.settings.capture_periods, str(p))
        H.cmd("capture_stop"); H.wait("capture_stopped", timeout=10, since=m)
    finally:
        H.close()
    # ストリームの音量判定(偽の parec)
    from miyamizu import pwutil
    loud = textwrap.dedent('''
    import sys, time, numpy as np
    t=time.monotonic()
    while time.monotonic()-t<2:
        sys.stdout.buffer.write((np.sin(np.arange(800)/5)*12000).astype("<i2").tobytes()); sys.stdout.buffer.flush(); time.sleep(0.02)''')
    quiet = "import sys,time\nt=time.monotonic()\nwhile time.monotonic()-t<2:\n    sys.stdout.buffer.write(bytes(1600)); sys.stdout.buffer.flush(); time.sleep(0.02)"
    open("/tmp/lv_loud.py", "w").write(loud); open("/tmp/lv_quiet.py", "w").write(quiet)
    sts = [{"index": 1, "sink_name": "s", "app": "A"}, {"index": 2, "sink_name": "s", "app": "B"},
           {"index": 3, "sink_name": "s", "app": "C", "corked": True}]
    pwutil.probe_levels(sts, 0.4, cmd_for=lambda st: [sys.executable, "/tmp/lv_loud.py" if st["index"] == 1 else "/tmp/lv_quiet.py"])
    check("鳴っているストリームを判別", sts[0]["active"] and not sts[1]["active"] and not sts[2]["active"],
          str([(s.get("level_db"), s.get("active")) for s in sts]))
    print("ALL PASS" if not fails else f"{fails} FAIL")


if __name__ == "__main__":
    main()
