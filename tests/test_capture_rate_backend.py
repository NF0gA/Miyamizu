"""アプリ音声: 取り込みレートの自動合わせ(DAC 非対応なら固定値へ)、pw-cat の自動フォールバック、遅延目標の自動調整。"""
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


def main():
    open("/tmp/gen_cap.py", "w").write(GEN)
    os.environ.pop("MIYAMIZU_CAPTURE_CMD", None)
    H = Harness("capture-paced", "S32_LE", settings=dict(s32_valid_bits=32, capture_rate=48000, capture_latency_ms=40, idle_close_s=0))
    eng = H.eng
    from miyamizu import engine as E, pwutil
    from miyamizu.settings import Settings
    fails = 0
    def check(name, ok, extra=""):
        nonlocal fails
        print(("PASS " if ok else "FAIL ") + name, extra); fails += (not ok)
    try:
        E.shutil_which = lambda n: "/usr/bin/" + n
        calls = []
        def begin(si, rate, ch=2):
            calls.append(("begin", rate))
            return {"module": 1, "sink_input": (si if isinstance(si, int) else si[0]), "inputs": list(si) if isinstance(si, list) else [si],
                    "prev_sink": "dac", "monitor": "m.monitor", "keys": set(), "touched": set()}
        pwutil.capture_begin = begin
        pwutil.capture_end = lambda st, dest=None: calls.append(("end", None)) if st else None
        pwutil.list_app_streams = lambda exclude_pids=None: []
        pwutil.stream_rate = lambda i: 44100
        gen_cmd = [sys.executable, "/tmp/gen_cap.py"]
        pwutil.capture_cmd = lambda backend, mon, rate: (["true"] if backend == "pw-cat" else gen_cmd)
        real_prep = eng._prepare_session
        base = dict(s32_valid_bits=32, capture_rate=48000, idle_close_s=0)

        # 1) アプリのレートに合わせる
        eng.settings = Settings.from_dict(dict(base, capture_rate_auto=True))
        eng.test_mode = True
        orig = eng.test_mode
        eng.start_capture([5], "a")
        check("アプリのレート(44.1k)で取り込む", calls[:1] == [("begin", 44100)] and eng._cap and eng._cap["rate"] == 44100, str(calls))
        eng._end_capture()
        # 2) DAC が受けられなければ設定の固定レートで取り直す
        calls.clear()
        def prep(info):
            plan = real_prep(info)
            if info["rate"] == 44100:
                plan.out_rate = 48000                       # 44.1k は非対応という想定
            return plan
        eng._prepare_session = prep
        eng.start_capture([5], "a")
        check("非対応なら 44.1k → 48k で取り直す", calls == [("begin", 44100), ("end", None), ("begin", 48000)] and eng._cap and eng._cap["rate"] == 48000, str(calls))
        eng._end_capture()
        eng._prepare_session = real_prep
        # 3) 自動合わせオフなら固定値
        calls.clear()
        eng.settings = Settings.from_dict(dict(base, capture_rate_auto=False))
        eng.start_capture([5], "a")
        check("自動オフなら設定レート", calls[:1] == [("begin", 48000)], str(calls))
        eng._end_capture()
        # 4) pw-cat で届かなければ parec へ自動で切り替え
        calls.clear()
        eng.settings = Settings.from_dict(dict(base, capture_rate_auto=False, capture_backend="pw-cat"))
        m = H.mark()
        eng.start_capture([5], "テスト")
        check("pw-cat で開始", eng._cap and eng._cap["backend"] == "pw-cat")
        time.sleep(0.5)
        eng._cap["t0"] -= 10
        eng._capture_watchdog()
        time.sleep(0.5)
        check("届かないと parec で取り込み直す", eng._cap and eng._cap["backend"] == "parec", str(eng._cap and eng._cap.get("backend")))
        check("通知が出る", any(e["ev"] == "notice" and "parec" in e["msg"] for e in H.events[m:]))
        time.sleep(1.0)
        check("parec 側では音が届く", eng._cap["src"].shm.write_pos > 0)
        # 5) 遅延目標の自動調整
        eng.settings = Settings.from_dict(dict(base, capture_latency_auto=True, capture_latency_min_ms=20, capture_latency_max_ms=200))
        cap = eng._cap
        cap["target_ms"] = 40.0
        now = time.monotonic()
        cap["adapt_t"], cap["calm_t"] = now - 10, now - 10
        tgt = int(cap["rate"] * 0.040)
        eng._on_live_stats(cap["sid"], {"fill_min": 10, "fill_max": tgt, "target": tgt, "ppm": 0, "dropped": 0, "duped": 0, "sync": "ratio"})
        check("溜まりが枯れそうなら目標を増やす", cap["target_ms"] > 40.0, f"{cap['target_ms']}")
        up = cap["target_ms"]
        now = time.monotonic()
        cap["adapt_t"], cap["calm_t"] = now - 20, now - 40
        tgt = int(cap["rate"] * up / 1000)
        eng._on_live_stats(cap["sid"], {"fill_min": int(tgt * 0.9), "fill_max": tgt, "target": tgt, "ppm": 0, "dropped": 0, "duped": 0, "sync": "ratio"})
        check("長く余裕があれば少し減らす", cap["target_ms"] < up, f"{up} -> {cap['target_ms']}")
        cap["target_ms"] = 20.0
        cap["adapt_t"], cap["calm_t"] = time.monotonic() - 20, time.monotonic() - 40
        tgt = int(cap["rate"] * 0.02)
        eng._on_live_stats(cap["sid"], {"fill_min": tgt, "fill_max": tgt, "target": tgt, "ppm": 0, "dropped": 0, "duped": 0, "sync": "ratio"})
        check("下限より下げない", cap["target_ms"] >= 20.0, f"{cap['target_ms']}")
        eng.settings = Settings.from_dict(dict(base, capture_latency_auto=False))
        cap["target_ms"] = 40.0
        cap["adapt_t"], cap["calm_t"] = time.monotonic() - 20, time.monotonic() - 40
        eng._on_live_stats(cap["sid"], {"fill_min": 1, "fill_max": 2, "target": 1, "ppm": 0, "dropped": 0, "duped": 0, "sync": "ratio"})
        check("自動オフなら動かさない", cap["target_ms"] == 40.0)
        eng._end_capture()
    finally:
        H.close()
    print("ALL PASS" if not fails else f"{fails} FAIL")
    sys.exit(1 if fails else 0)


if __name__ == "__main__":
    main()
