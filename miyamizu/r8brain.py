"""r8brain-free-src(Voxengo) リサンプラ。C++ ヘッダオンリーの本体を C ABI で包んだ共有ライブラリ(native/r8b_shim.cpp)を ctypes で呼ぶ。

Sample rate converter designed by Aleksey Vaneev of Voxengo (MIT License)

ライブラリは 同梱の libr8bshim.so → 無ければ g++ でその場ビルド(キャッシュ)の順に探す。
使えなければ available() が False になり、呼び出し側は soxr へ戻る。
"""
from __future__ import annotations

import ctypes
import os
import shutil
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import numpy as np

SONAME = "libr8bshim.dll" if sys.platform == "win32" else "libr8bshim.so"
_D = ctypes.POINTER(ctypes.c_double)
_lib = None
_tried = False

# プリセット: (遷移帯域[%], 阻止域減衰[dB], 位相)
PRESETS: dict[str, tuple[float, float, str]] = {
    "standard": (2.0, 206.91, "linear"),
    "minphase": (2.0, 206.91, "minimum"),
    "fast": (10.0, 120.0, "linear"),
}
TB_RANGE = (0.5, 45.0)
ATT_RANGE = (49.0, 218.0)
PHASES = ("linear", "minimum")


def params(preset: str, tb: float, att: float, phase: str) -> tuple[float, float, str]:
    """設定値 → (遷移帯域, 減衰, 位相)。custom は手動値(範囲内に丸める)。"""
    if preset in PRESETS:
        return PRESETS[preset]
    tb = min(max(float(tb), TB_RANGE[0]), TB_RANGE[1])
    att = min(max(float(att), ATT_RANGE[0]), ATT_RANGE[1])
    return tb, att, phase if phase in PHASES else "linear"


def _candidates() -> list[Path]:
    here = Path(__file__).resolve().parent
    out = [here / SONAME, here / "native" / SONAME]
    if getattr(sys, "frozen", False):
        out.insert(0, Path(sys.executable).resolve().parent / SONAME)
    mp = getattr(sys, "_MEIPASS", None)
    if mp:
        out += [Path(mp) / SONAME, Path(mp) / "miyamizu" / SONAME]
    return out


def _source_dir() -> Path | None:
    here = Path(__file__).resolve().parent
    for d in (here / "native", Path(getattr(sys, "_MEIPASS", "")) / "miyamizu" / "native"):
        if (d / "r8b_shim.cpp").is_file() and (d / "r8brain-free-src").is_dir():
            return d
    return None


def _build() -> Path | None:
    cxx = shutil.which("g++") or shutil.which("c++") or shutil.which("clang++")
    d = _source_dir()
    if not cxx or not d:
        return None
    from .common import cache_dir

    cd = cache_dir()
    cd.mkdir(parents=True, exist_ok=True)
    so = cd / SONAME
    try:
        newest = max(p.stat().st_mtime for p in [d / "r8b_shim.cpp", *(d / "r8brain-free-src").glob("*.h")])
        if so.is_file() and so.stat().st_mtime >= newest:
            return so
        tmp = so.with_suffix(".tmp")
        flags = ["-static", "-static-libgcc", "-static-libstdc++"] if sys.platform == "win32" else ["-fPIC", "-fvisibility=hidden"]
        subprocess.run([cxx, "-O3", "-std=c++17", "-shared", *flags, f"-I{d / 'r8brain-free-src'}",
                        str(d / "r8b_shim.cpp"), "-o", str(tmp)], check=True,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=180)
        os.replace(tmp, so)
        return so
    except (OSError, subprocess.SubprocessError):
        return None


def _load():
    global _lib, _tried
    if _tried:
        return _lib
    _tried = True
    paths = [p for p in _candidates() if p.is_file()]
    b = None if paths else _build()
    if b:
        paths.append(b)
    for p in paths:
        try:
            lib = ctypes.CDLL(str(p))
            lib.r8bs_convert.argtypes = [ctypes.c_double] * 4 + [ctypes.c_int, _D, ctypes.c_long, _D, ctypes.c_long]
            lib.r8bs_convert.restype = ctypes.c_int
            lib.r8bs_new.argtypes = [ctypes.c_double, ctypes.c_double, ctypes.c_int, ctypes.c_double, ctypes.c_double,
                                     ctypes.c_int]
            lib.r8bs_new.restype = ctypes.c_void_p
            lib.r8bs_process.argtypes = [ctypes.c_void_p, _D, ctypes.c_int, _D, ctypes.c_int]
            lib.r8bs_process.restype = ctypes.c_int
            lib.r8bs_free.argtypes = [ctypes.c_void_p]
            lib.r8bs_free.restype = None
            lib.r8bs_version.restype = ctypes.c_char_p
            _lib = lib
            break
        except (OSError, AttributeError):
            continue
    return _lib


def available() -> bool:
    return _load() is not None


def version() -> str:
    lib = _load()
    return lib.r8bs_version().decode() if lib else ""


_POOL: ThreadPoolExecutor | None = None


def _pool() -> ThreadPoolExecutor:
    global _POOL
    if _POOL is None:
        _POOL = ThreadPoolExecutor(max_workers=max(2, min(8, os.cpu_count() or 2)))
    return _POOL


_PH = {"linear": 0, "minimum": 1}


class R8Stream:
    """soxr_ctypes.SoxrStream と同じ resample_chunk(x(n,ch) float64, last) → (m,ch) float64。
    チャンネルごとに別スレッドで処理する(ctypes は GIL を解放)。"""

    def __init__(self, in_rate: float, out_rate: float, channels: int, trans_band: float, atten_db: float,
                 phase: str, max_in: int = 8192):
        lib = _load()
        if lib is None:
            raise RuntimeError("libr8bshim を読み込めません")
        self._lib = lib
        self.ch = channels
        self.ratio = out_rate / in_rate
        self.max_in = int(max_in)
        self._hs = []
        for _ in range(channels):
            h = lib.r8bs_new(in_rate, out_rate, self.max_in, trans_band, atten_db, _PH.get(phase, 0))
            if not h:
                self.close()
                raise RuntimeError("r8brain の初期化に失敗")
            self._hs.append(h)
        self.in_total = 0
        self.out_total = 0

    def _run(self, i: int, col: np.ndarray, cap: int) -> np.ndarray:
        out = np.empty(cap, dtype=np.float64)
        k = self._lib.r8bs_process(self._hs[i], col.ctypes.data_as(_D), len(col), out.ctypes.data_as(_D), cap)
        if k < 0:
            raise RuntimeError(f"r8brain process failed ({k})")
        return out[:k]

    def _feed(self, x: np.ndarray) -> np.ndarray:
        n = x.shape[0]
        cap = int(n * self.ratio) + 128
        cols = [np.ascontiguousarray(x[:, i]) for i in range(self.ch)]
        if self.ch > 1 and n >= 256:
            res = list(_pool().map(lambda i: self._run(i, cols[i], cap), range(self.ch)))
        else:
            res = [self._run(i, cols[i], cap) for i in range(self.ch)]
        m = len(res[0])
        y = np.empty((m, self.ch), dtype=np.float64)
        for i, r in enumerate(res):
            y[:, i] = r
        return y

    def resample_chunk(self, x: np.ndarray, last: bool = False) -> np.ndarray:
        x = np.asarray(x, dtype=np.float64)
        outs: list[np.ndarray] = []
        for pos in range(0, x.shape[0], self.max_in):
            blk = x[pos:pos + self.max_in]
            y = self._feed(blk)
            self.in_total += blk.shape[0]
            self.out_total += y.shape[0]
            outs.append(y)
        if last:
            want = int(round(self.in_total * self.ratio))
            guard = 0
            while self.out_total < want and guard < 10000:
                guard += 1
                y = self._feed(np.zeros((self.max_in, self.ch)))
                take = min(y.shape[0], want - self.out_total)
                self.out_total += take
                outs.append(y[:take])
            if self.out_total > want:     # 入力のみで超過した場合の端数を切る
                extra = self.out_total - want
                tail = outs[-1] if outs else None
                if tail is not None and tail.shape[0] >= extra:
                    outs[-1] = tail[:tail.shape[0] - extra]
                    self.out_total = want
        if not outs:
            return np.zeros((0, self.ch), dtype=np.float64)
        return np.concatenate(outs, axis=0) if len(outs) > 1 else outs[0]

    def close(self):
        lib = getattr(self, "_lib", None)
        for h in getattr(self, "_hs", []):
            if h and lib:
                lib.r8bs_free(h)
        self._hs = []

    def __del__(self):
        try:
            self.close()
        except Exception:  # noqa: BLE001
            pass


def convert(x: np.ndarray, src: float, dst: float, *, trans_band: float = 2.0, atten_db: float = 206.91,
            phase: str = "linear", out_dtype=np.float32) -> np.ndarray:
    """曲全体を一括変換(検証・事前変換向け)。x: (frames,) か (frames, ch)。同レートは変換しない。"""
    lib = _load()
    if lib is None:
        raise RuntimeError("libr8bshim を読み込めません")
    if src == dst:
        return x.astype(out_dtype, copy=False)
    mono = x.ndim == 1
    if mono:
        x = x[:, None]

    def one(i):
        a = np.ascontiguousarray(x[:, i], dtype=np.float64)
        cap = int(round(len(a) * dst / src)) + 16
        out = np.empty(cap, dtype=np.float64)
        n = lib.r8bs_convert(src, dst, trans_band, atten_db, _PH.get(phase, 0), a.ctypes.data_as(_D), len(a),
                             out.ctypes.data_as(_D), cap)
        if n < 0:
            raise RuntimeError(f"r8brain convert failed ({n})")
        return out[:n]

    chans = list(_pool().map(one, range(x.shape[1])))
    y = np.stack(chans, axis=1).astype(out_dtype, copy=False)
    return y[:, 0] if mono else y
