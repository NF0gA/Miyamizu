"""ノイズシェーピング付き再量子化(C 実装を ctypes で呼ぶ)。

ビット深度を落とす(例: 32bit/浮動小数 → 24bit/16bit)時の量子化ノイズを、聞こえにくい高域へ寄せる。
C ライブラリは、同梱の .so → 無ければ cc でその場でビルド(キャッシュ)の順に探す。使えなければ None を返し、通常の TPDF ディザへ戻る。
"""
from __future__ import annotations

import ctypes
import os
import shutil
import subprocess
import sys
from pathlib import Path

import numpy as np

# NTF(z) = 1 - sum c[k] z^-(k+1)(SoX の係数。44.1kHz 向け)
FILTERS: dict[str, tuple[str, list[float]]] = {
    "first": ("1次(DC から離して高域へ。軽い)", [1.0]),
    "lipshitz": ("Lipshitz 5次(E特性・耳の感度の高い2〜5kHzを下げる)", [2.033, -2.165, 1.959, -1.590, 0.6149]),
    "f_weighted": ("F特性 9次(さらに深く下げる。高域のノイズは増える)",
                   [2.412, -3.370, 3.937, -4.174, 3.353, -2.205, 1.281, -0.569, 0.0847]),
    "improved_e": ("改良E特性 9次(最も深いが高域ノイズが大きい)",
                   [2.847, -4.685, 6.214, -7.184, 6.639, -5.032, 3.263, -1.632, 0.4191]),
}

_lib = None
_tried = False
_SO = "libmiyamizu_nshape.dll" if sys.platform == "win32" else "libmiyamizu_nshape.so"


def _candidates() -> list[Path]:
    here = Path(__file__).resolve().parent
    out = [here / _SO]
    if getattr(sys, "frozen", False):
        out.insert(0, Path(sys.executable).resolve().parent / _SO)
    mp = getattr(sys, "_MEIPASS", None)
    if mp:
        out.append(Path(mp) / _SO)
        out.append(Path(mp) / "miyamizu" / _SO)
    return out


def _source() -> Path | None:
    here = Path(__file__).resolve().parent
    for p in (here / "nshape.c", Path(getattr(sys, "_MEIPASS", "")) / "miyamizu" / "nshape.c"):
        if p.is_file():
            return p
    return None


def _build() -> Path | None:
    cc = shutil.which("cc") or shutil.which("gcc") or shutil.which("clang")
    src = _source()
    if not cc or not src:
        return None
    from .common import cache_dir

    d = cache_dir()
    d.mkdir(parents=True, exist_ok=True)
    so = d / _SO
    try:
        if so.is_file() and so.stat().st_mtime >= src.stat().st_mtime:
            return so
        subprocess.run([cc, "-O2", "-shared", *([] if sys.platform == "win32" else ["-fPIC"]), str(src), "-o", str(so), "-lm"], check=True,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=60)
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
            lib.nshape_run.restype = ctypes.c_int
            lib.nshape_run.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_long, ctypes.c_int, ctypes.c_int,
                                       ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_int]
            _lib = lib
            break
        except OSError:
            continue
    return _lib


def available() -> bool:
    return _load() is not None


class NoiseShaper:
    """1ストリーム分の状態(誤差履歴・乱数)を持つ量子化器。チャンクを順に渡す。"""

    def __init__(self, name: str, channels: int, valid_bits: int, dither: bool = True, seed: int = 1):
        if name not in FILTERS:
            raise ValueError(name)
        if _load() is None:
            raise RuntimeError("nshape ライブラリを読み込めません")
        self.c = np.ascontiguousarray(FILTERS[name][1], dtype=np.float64)
        self.taps = len(self.c)
        self.ch = channels
        self.bits = int(valid_bits)
        self.dither = 1 if dither else 0
        self.hist = np.zeros((channels, self.taps), dtype=np.float64)
        self.rng = (np.arange(channels, dtype=np.uint64) * np.uint64(0x9E3779B97F4A7C15) + np.uint64(seed * 2654435761 + 88172645463325252))
        self.rng[self.rng == 0] = 88172645463325252
        self.clips = 0

    def quantize(self, x: np.ndarray) -> np.ndarray:
        """float64 (frames, ch) [-1,1) → valid_bits に量子化した left-justified int32。"""
        x = np.ascontiguousarray(x, dtype=np.float64)
        n = x.shape[0]
        out = np.empty((n, self.ch), dtype=np.int32)
        if n:
            self.clips += _lib.nshape_run(x.ctypes.data, out.ctypes.data, n, self.ch, self.bits, self.c.ctypes.data, self.taps,
                                          self.hist.ctypes.data, self.rng.ctypes.data, self.dither)
        return out
