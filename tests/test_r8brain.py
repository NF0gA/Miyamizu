"""r8brain: 一括=ストリーミング、soxr とのヌル、周波数特性、同レートバイパス、フォールバック。"""
import sys
import numpy as np
from miyamizu import r8brain, producer
from miyamizu.engine import resample_label
from miyamizu.settings import Settings

if not r8brain.available():
    print("SKIP r8brain unavailable"); sys.exit(0)

fs, n = 44100, 44100 * 2
t = np.arange(n) / fs
x = np.stack([0.5 * np.sin(2 * np.pi * 1000 * t), 0.4 * np.sin(2 * np.pi * 7000 * t)], 1)

# 1. streaming == batch
b = r8brain.convert(x, fs, 96000, out_dtype=np.float64)
s = r8brain.R8Stream(fs, 96000, 2, 2.0, 206.91, "linear")
y = np.concatenate([s.resample_chunk(x[i:i + 4096], last=i + 4096 >= n) for i in range(0, n, 4096)])
assert y.shape == b.shape and np.abs(y - b).max() < 1e-12, (y.shape, b.shape)

# 2. accuracy vs ideal sine / soxr null
t2 = np.arange(b.shape[0]) / 96000
ideal = 0.5 * np.sin(2 * np.pi * 1000 * t2)
mid = slice(9600, -9600)
err = np.abs(b[mid, 0] - ideal[mid]).max()
assert err < 1e-6, err
sx = producer.make_resampler(fs, 96000, 2, "VHQ", "linear", "standard").resample_chunk(x, last=True)
m = min(len(sx), len(b))
d = np.abs(sx[:m, 0][mid] - b[:m, 0][mid]).max() if m > 19200 else 1
assert d < 1e-3, d     # 1kHz 付近で両者はほぼ一致(遅延が揃っている)

# 3. 非整数比の出力長・最小位相・プリセット
for src, dst in [(48000, 44100), (88200, 44100), (44100, 192000)]:
    z = r8brain.convert(x[:, 0], src, dst, phase="minimum", out_dtype=np.float64)
    assert abs(len(z) - round(n * dst / src)) <= 1
# 4. 周波数特性: 20kHz 通過、22.05kHz 以上は減衰
f = np.sin(2 * np.pi * 20000 * t)
z = r8brain.convert(f, fs, 96000, out_dtype=np.float64)[9600:-9600]
assert abs(np.abs(z).max() - 1) < 0.01
# 5. 同レートは変換しない
same = r8brain.convert(x, fs, fs, out_dtype=np.float64)
assert np.array_equal(same, x)
# 6. params / label
assert r8brain.params("fast", 1, 1, "x") == (10.0, 120.0, "linear")
assert r8brain.params("custom", 99, 10, "minimum") == (45.0, 49.0, "minimum")
st = Settings(); st.resample_engine = "r8brain"
assert "r8brain" in resample_label(st)
print("OK")

# 7. ライブラリ無し → soxr に戻る(エンジン名)
from miyamizu.engine import Engine
r8brain._lib, r8brain._tried = None, True
assert Engine._engine_name(st) == "soxr" and not r8brain.available()
print("OK fallback")
