"""libsoxr を ctypes で直接呼ぶリサンプラ(python-soxr では選べない「位相特性」「帯域」を指定するため)。

python-soxr は品質(QQ..VHQ)しか指定できないが、libsoxr 本体は
  - 位相: 線形 / 中間 / 最小
  - 帯域: 標準(約91%) / 広帯域(STEEP, 約98%)
  - 精度: 16〜32bit
を選べる。ここでは同じ `resample_chunk(x, last)` インターフェースで包む。
libsoxr が無い環境では `available()` が False になり、呼び出し側は python-soxr に戻る。
"""
from __future__ import annotations

import ctypes
import ctypes.util
from ctypes import POINTER, Structure, byref, c_char_p, c_double, c_size_t, c_uint, c_ulong, c_void_p

import numpy as np

# --- soxr.h の定数 ---
RECIPES = {"QQ": 0, "LQ": 1, "MQ": 2, "HQ": 4, "VHQ": 6, "32": 7}     # SOXR_*_BITQ (HQ=20bit, VHQ=28bit)
PHASE_FLAGS = {"linear": 0x00, "intermediate": 0x10, "minimum": 0x30}
SOXR_STEEP_FILTER = 0x40
SOXR_FLOAT64_I = 1


class IoSpec(Structure):
    _fields_ = [("itype", c_uint), ("otype", c_uint), ("scale", c_double), ("e", c_void_p), ("flags", c_ulong)]


class QualitySpec(Structure):
    _fields_ = [("precision", c_double), ("phase_response", c_double), ("passband_end", c_double),
                ("stopband_begin", c_double), ("e", c_void_p), ("flags", c_ulong)]


class RuntimeSpec(Structure):
    _fields_ = [("log2_min_dft_size", c_uint), ("log2_large_dft_size", c_uint), ("coef_size_kbytes", c_uint),
                ("num_threads", c_uint), ("e", c_void_p), ("flags", c_ulong)]


_lib = None
_lib_err: str | None = None


def _load():
    global _lib, _lib_err
    if _lib is not None or _lib_err is not None:
        return _lib
    names = [ctypes.util.find_library("soxr"), "libsoxr.so.0", "libsoxr.so"]
    for n in names:
        if not n:
            continue
        try:
            lib = ctypes.CDLL(n)
            lib.soxr_create.restype = c_void_p
            lib.soxr_create.argtypes = [c_double, c_double, c_uint, POINTER(c_char_p), POINTER(IoSpec),
                                        POINTER(QualitySpec), POINTER(RuntimeSpec)]
            lib.soxr_process.restype = c_char_p      # soxr_error_t (const char*) — NULL なら成功
            lib.soxr_process.argtypes = [c_void_p, c_void_p, c_size_t, POINTER(c_size_t), c_void_p, c_size_t,
                                         POINTER(c_size_t)]
            lib.soxr_delete.argtypes = [c_void_p]
            lib.soxr_delete.restype = None
            lib.soxr_quality_spec.restype = QualitySpec
            lib.soxr_quality_spec.argtypes = [c_ulong, c_ulong]
            lib.soxr_io_spec.restype = IoSpec
            lib.soxr_io_spec.argtypes = [c_uint, c_uint]
            lib.soxr_runtime_spec.restype = RuntimeSpec
            lib.soxr_runtime_spec.argtypes = [c_uint]
            _lib = lib
            return _lib
        except (OSError, AttributeError) as e:
            _lib_err = _lib_err or str(e)
    _lib_err = _lib_err or "libsoxr が見つかりません"
    return None


def available() -> bool:
    return _load() is not None


def unavailable_reason() -> str:
    _load()
    return _lib_err or ""


class SoxrStream:
    """float64 インターリーブ (n, ch) を流し込むストリーミング・リサンプラ。"""

    def __init__(self, in_rate: float, out_rate: float, channels: int, quality: str = "VHQ",
                 phase: str = "linear", steep: bool = False, passband_end: float | None = None):
        lib = _load()
        if lib is None:
            raise RuntimeError("libsoxr が使えません: " + (_lib_err or ""))
        self._lib = lib
        self.ch = channels
        self.ratio = out_rate / in_rate
        recipe = RECIPES.get(quality, RECIPES["VHQ"])
        flags = PHASE_FLAGS.get(phase, 0) | (SOXR_STEEP_FILTER if steep else 0)
        q = lib.soxr_quality_spec(recipe | flags, 0)
        if passband_end:
            q.passband_end = float(passband_end)
        io = lib.soxr_io_spec(SOXR_FLOAT64_I, SOXR_FLOAT64_I)
        rt = lib.soxr_runtime_spec(1)          # スレッドは使わない(専用コア運用と再現性のため)
        err = c_char_p(None)
        self._h = lib.soxr_create(float(in_rate), float(out_rate), channels, byref(err), byref(io), byref(q), byref(rt))
        if err.value or not self._h:
            msg = err.value.decode(errors="replace") if err.value else "soxr_create failed"
            raise RuntimeError(f"libsoxr: {msg}")
        self.quality_spec = (q.precision, q.phase_response, q.passband_end, q.stopband_begin)

    def _call(self, inp, ilen: int, out: np.ndarray) -> tuple[int, int]:
        idone, odone = c_size_t(0), c_size_t(0)
        err = self._lib.soxr_process(self._h, inp, ilen, byref(idone), out.ctypes.data_as(c_void_p), out.shape[0],
                                     byref(odone))
        if err:
            raise RuntimeError("libsoxr: " + err.decode(errors="replace"))
        return idone.value, odone.value

    def resample_chunk(self, x: np.ndarray, last: bool = False) -> np.ndarray:
        x = np.ascontiguousarray(x, dtype=np.float64)
        n = x.shape[0]
        outs: list[np.ndarray] = []
        done = 0
        cap = int(n * self.ratio) + 4096
        while done < n:
            buf = np.empty((cap, self.ch), dtype=np.float64)
            ptr = (x.ctypes.data + done * self.ch * 8) if n else None
            idone, odone = self._call(ptr, n - done, buf)
            outs.append(buf[:odone])
            done += idone
            if idone == 0 and odone == 0:
                break
        if last:
            while True:
                buf = np.empty((max(cap, 65536), self.ch), dtype=np.float64)
                _, odone = self._call(None, 0, buf)      # 入力終端: 内部に残った分を吐き出す
                if odone == 0:
                    break
                outs.append(buf[:odone])
        if not outs:
            return np.zeros((0, self.ch), dtype=np.float64)
        return np.concatenate(outs, axis=0) if len(outs) > 1 else outs[0]

    def close(self):
        if getattr(self, "_h", None):
            self._lib.soxr_delete(self._h)
            self._h = None

    def __del__(self):
        try:
            self.close()
        except Exception:  # noqa: BLE001
            pass
