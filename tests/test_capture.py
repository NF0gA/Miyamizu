"""アプリ音声の取り込み: parec の代わりに偽のraw s32le(速めのクロック)を流し、素通し・連続性・停止/復帰を確認。"""
import os, sys, time, textwrap
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import numpy as np
from harness import Harness, left_i32_from_stream, find_start


GEN = textwrap.dedent('''
import sys, time, numpy as np
rate=48000; chunk=480; ppm=1.002
t0=time.monotonic(); n=0
out=sys.stdout.buffer
while True:
    k=np.arange(n,n+chunk)
    v=(np.sin(2*np.pi*440*k/rate)*0.5*2**31).astype('<i4')
    out.write(np.stack([v,v],1).tobytes()); out.flush()
    n+=chunk
    due=t0+n/rate/ppm
    d=due-time.monotonic()
    if d>0: time.sleep(d)
''')


def main():
    open("/tmp/gen_cap.py", "w").write(GEN)
    os.environ["MIYAMIZU_CAPTURE_CMD"] = f"{sys.executable} /tmp/gen_cap.py"
    H = Harness("capture-paced", "S32_LE", settings=dict(s32_valid_bits=32, capture_rate=48000, capture_latency_ms=80, idle_close_s=0))
    fails = 0
    def check(name, ok, extra=""):
        nonlocal fails
        print(("PASS " if ok else "FAIL ") + name, extra); fails += (not ok)
    try:
        m = H.mark()
        H.cmd("capture_start", sink_input=1, label="テストアプリ")
        H.wait("capture_started", timeout=10, since=m)
        ev = H.wait("track_changed", timeout=10, since=m)
        sig = ev["signal"]
        check("シグナルパス: ライブ入力/RAM無効/処理なし", sig["ram"] is False and sig["steps"] == [] and "アプリ音声" in sig["source"], str(sig["source"]))
        time.sleep(4.0)
        H.cmd("capture_stop")
        H.wait("capture_stopped", timeout=10, since=m)
        x = left_i32_from_stream(H.captured(), "S32_LE", 2)
        s0 = find_start(x)
        y = x[s0:, 0].astype(np.float64) / 2**31
        nz = np.nonzero(np.abs(y) > 1e-9)[0]
        y = y[:nz[-1] + 1] if len(nz) else y      # 停止後に出力が埋める無音は除く
        check("取り込んだ長さ(約4秒 - 初期バッファ)", 2.8 < len(y) / 48000 < 4.6, f"{len(y)/48000:.2f}s")
        d = np.abs(np.diff(y))
        # 440Hz 振幅0.5 の正弦の最大隣接差は ~0.0288。1フレームの重複/脱落でも ~0.06 以内
        check("波形が連続(大きな不連続なし)", d.max() < 0.1, f"max step={d.max():.4f}")
        dup_or_drop = int((d > 0.0295).sum() + (d < 1e-6).sum())
        check("クロック差の調整は少数フレーム", dup_or_drop < 60, f"adjusted~{dup_or_drop}")
        check("停止後は再生中でない", H.eng._cap is None and H.eng.state == "stopped")
        # 通常曲へ切替えで取り込みが終わる
        m2 = H.mark(); H.cmd("capture_start", sink_input=1, label="x"); H.wait("capture_started", timeout=10, since=m2)
        H.cmd("stop"); H.wait("capture_stopped", timeout=10, since=m2)
        check("stop コマンドで取り込み終了", H.eng._cap is None)
    finally:
        H.close()
    print("ALL PASS" if not fails else f"{fails} FAIL")


if __name__ == "__main__":
    main()
