"""サンプル形式の変換。

内部表現:
  * 整数パス : int32 配列 (frames, ch)。最上位ビットに揃えた "left-justified"(16bit なら <<16)
  * 浮動小数 : float64 配列 (frames, ch)、範囲 [-1, 1)
出力はデバイス形式 (S32_LE / S24_3LE / S24_LE / S16_LE) のバイト列 (uint8, フラット)。

ビットパーフェクト: 整数パスで有効ビット数が足りる限り、値は一切変更されない。
有効ビット数が足りない(例: 32bit PCM → 24bit出力)場合だけ丸め(+任意でTPDFディザ)を行う。
"""
from __future__ import annotations

import numpy as np

# 形式名 -> (1サンプルのバイト数, 既定の有効ビット数)
FORMATS = {
    "S32_LE": (4, 24),
    "S24_3LE": (3, 24),
    "S24_LE": (4, 24),
    "S16_LE": (2, 16),
}
# ALSA の snd_pcm_format_t
ALSA_FORMAT_ENUM = {"S16_LE": 2, "S24_LE": 6, "S32_LE": 10, "S24_3LE": 32}
# 優先順 (ハード直結で最も情報を落とさない順)
FORMAT_PREFERENCE = ["S32_LE", "S24_3LE", "S24_LE", "S16_LE"]


def bytes_per_sample(fmt: str) -> int:
    return FORMATS[fmt][0]


def default_valid_bits(fmt: str, s32_valid_bits: int = 24) -> int:
    if fmt == "S32_LE":
        return s32_valid_bits
    return FORMATS[fmt][1]


def i32_to_float(x: np.ndarray) -> np.ndarray:
    return x.astype(np.float64) * (1.0 / 2147483648.0)


def _tpdf(rng: np.random.Generator, shape) -> np.ndarray:
    return rng.random(shape) - rng.random(shape)


def float_to_left_i32(x: np.ndarray, valid_bits: int, dither: bool, rng: np.random.Generator | None) -> np.ndarray:
    """float64 [-1,1) → valid_bits に量子化して left-justified int32 にする。"""
    scale = float(1 << (valid_bits - 1))
    y = x * scale
    if dither and valid_bits < 32:
        y = y + _tpdf(rng or np.random.default_rng(), y.shape)
    q = np.rint(y)
    np.clip(q, -scale, scale - 1, out=q)
    return (q.astype(np.int64) << (32 - valid_bits)).astype(np.int32)


def requantize_left_i32(x: np.ndarray, valid_bits: int, dither: bool, rng: np.random.Generator | None, shaper=None) -> np.ndarray:
    """left-justified int32 を valid_bits に揃える。下位ビットが既に 0 なら無変更(ビットパーフェクト)。"""
    if valid_bits >= 32:
        return x
    shift = 32 - valid_bits
    mask = (1 << shift) - 1
    if not np.any(x & mask):
        return x
    if shaper is not None:
        return shaper.quantize(i32_to_float(x))
    return float_to_left_i32(i32_to_float(x), valid_bits, dither, rng)


def pack(x: np.ndarray, fmt: str) -> np.ndarray:
    """left-justified int32 (frames, ch) をデバイス形式の uint8 フラット配列にする。"""
    x = np.ascontiguousarray(x, dtype="<i4")
    if fmt == "S32_LE":
        return x.view(np.uint8).reshape(-1)
    if fmt == "S24_LE":
        return (x >> 8).astype("<i4").view(np.uint8).reshape(-1)
    if fmt == "S16_LE":
        return (x >> 16).astype("<i2").view(np.uint8).reshape(-1)
    if fmt == "S24_3LE":
        b = x.view(np.uint8).reshape(-1, 4)[:, 1:4]
        return np.ascontiguousarray(b).reshape(-1)
    raise ValueError(f"unsupported format: {fmt}")


def unpack(buf: np.ndarray, fmt: str, channels: int) -> np.ndarray:
    """pack の逆変換(テスト用)。left-justified int32 (frames, ch) を返す。"""
    buf = np.asarray(buf, dtype=np.uint8)
    if fmt == "S32_LE":
        return buf.view("<i4").reshape(-1, channels).astype(np.int32)
    if fmt == "S24_LE":
        return (buf.view("<i4").reshape(-1, channels) << 8).astype(np.int32)
    if fmt == "S16_LE":
        return (buf.view("<i2").reshape(-1, channels).astype(np.int32) << 16)
    if fmt == "S24_3LE":
        b = buf.reshape(-1, 3)
        out = np.zeros((b.shape[0], 4), dtype=np.uint8)
        out[:, 1:4] = b
        return out.view("<i4").reshape(-1, channels).astype(np.int32)
    raise ValueError(fmt)
