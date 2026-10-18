"""libasound への ctypes バインディング(ビルド不要)。

- デバイス列挙 (/proc/asound)
- 対応レート/形式の問い合わせ
- hw_params による厳密なオープン (レート・形式・ピリオド/バッファ長)
- snd_pcm_writei によるブロッキング書き込み (ctypes は呼び出し中 GIL を解放する)
"""
from __future__ import annotations

import ctypes
import ctypes.util
import errno
import re
from dataclasses import dataclass, field
from pathlib import Path

from .common import STD_RATES
from .convert import ALSA_FORMAT_ENUM, FORMAT_PREFERENCE

SND_PCM_STREAM_PLAYBACK = 0
SND_PCM_ACCESS_RW_INTERLEAVED = 3

_lib = None


class AlsaError(OSError):
    pass


def lib():
    global _lib
    if _lib is None:
        name = ctypes.util.find_library("asound") or "libasound.so.2"
        _lib = ctypes.CDLL(name, use_errno=True)
        L = _lib
        vp, ci, cu, cul = ctypes.c_void_p, ctypes.c_int, ctypes.c_uint, ctypes.c_ulong
        L.snd_strerror.restype = ctypes.c_char_p
        L.snd_strerror.argtypes = [ci]
        L.snd_pcm_open.argtypes = [ctypes.POINTER(vp), ctypes.c_char_p, ci, ci]
        L.snd_pcm_close.argtypes = [vp]
        L.snd_pcm_hw_params_malloc.argtypes = [ctypes.POINTER(vp)]
        L.snd_pcm_hw_params_free.argtypes = [vp]
        L.snd_pcm_hw_params_any.argtypes = [vp, vp]
        L.snd_pcm_hw_params_set_rate_resample.argtypes = [vp, vp, cu]
        L.snd_pcm_hw_params_set_access.argtypes = [vp, vp, ci]
        L.snd_pcm_hw_params_set_format.argtypes = [vp, vp, ci]
        L.snd_pcm_hw_params_test_format.argtypes = [vp, vp, ci]
        L.snd_pcm_hw_params_set_channels.argtypes = [vp, vp, cu]
        L.snd_pcm_hw_params_set_rate.argtypes = [vp, vp, cu, ci]
        L.snd_pcm_hw_params_test_rate.argtypes = [vp, vp, cu, ci]
        L.snd_pcm_hw_params_set_period_time_near.argtypes = [vp, vp, ctypes.POINTER(cu), ctypes.POINTER(ci)]
        L.snd_pcm_hw_params_set_buffer_time_near.argtypes = [vp, vp, ctypes.POINTER(cu), ctypes.POINTER(ci)]
        L.snd_pcm_hw_params.argtypes = [vp, vp]
        L.snd_pcm_hw_params_get_period_size.argtypes = [vp, ctypes.POINTER(cul), ctypes.POINTER(ci)]
        L.snd_pcm_hw_params_get_buffer_size.argtypes = [vp, ctypes.POINTER(cul)]
        L.snd_pcm_hw_params_get_channels_min.argtypes = [vp, ctypes.POINTER(cu)]
        L.snd_pcm_hw_params_get_channels_max.argtypes = [vp, ctypes.POINTER(cu)]
        L.snd_pcm_sw_params_malloc.argtypes = [ctypes.POINTER(vp)]
        L.snd_pcm_sw_params_free.argtypes = [vp]
        L.snd_pcm_sw_params_current.argtypes = [vp, vp]
        L.snd_pcm_sw_params_set_start_threshold.argtypes = [vp, vp, cul]
        L.snd_pcm_sw_params_set_avail_min.argtypes = [vp, vp, cul]
        L.snd_pcm_sw_params.argtypes = [vp, vp]
        L.snd_pcm_prepare.argtypes = [vp]
        L.snd_pcm_writei.argtypes = [vp, vp, cul]
        L.snd_pcm_writei.restype = ctypes.c_long
        L.snd_pcm_recover.argtypes = [vp, ci, ci]
        L.snd_pcm_drain.argtypes = [vp]
        L.snd_pcm_drop.argtypes = [vp]
        L.snd_pcm_delay.argtypes = [vp, ctypes.POINTER(ctypes.c_long)]
    return _lib


def strerror(err: int) -> str:
    try:
        return lib().snd_strerror(err).decode(errors="replace")
    except Exception:  # noqa: BLE001
        return f"errno {err}"


def _chk(ret: int, what: str):
    if ret < 0:
        raise AlsaError(-ret, f"{what}: {strerror(ret)}")
    return ret


# ---------------------------------------------------------------- 列挙
@dataclass
class AlsaDevice:
    name: str           # "hw:CARD=X8,DEV=0"
    label: str
    card_index: int
    card_id: str


def list_hw_devices() -> list[AlsaDevice]:
    res: list[AlsaDevice] = []
    cards = Path("/proc/asound/cards")
    try:
        txt = cards.read_text()
    except OSError:
        return res
    for m in re.finditer(r"^\s*(\d+)\s+\[(\S*)\s*\]:\s*(.+?)\s*-\s*(.+)$", txt, re.M):
        idx, cid, drv, longname = int(m.group(1)), m.group(2), m.group(3), m.group(4)
        cdir = Path(f"/proc/asound/card{idx}")
        try:
            pcms = sorted(p.name for p in cdir.iterdir() if re.fullmatch(r"pcm\d+p", p.name))
        except OSError:
            pcms = []
        for p in pcms:
            dev = int(p[3:-1])
            res.append(AlsaDevice(f"hw:CARD={cid},DEV={dev}", f"{longname} ({drv}) hw:{idx},{dev}", idx, cid))
    return res


def usb_stream_info(card_index: int) -> str:
    """/proc/asound/cardN/stream0 の中身(USB オーディオの対応形式・転送方式・クロック)。"""
    try:
        return Path(f"/proc/asound/card{card_index}/stream0").read_text()
    except OSError:
        return ""


# ---------------------------------------------------------------- 問い合わせ
@dataclass
class Caps:
    device: str
    ok: bool
    error: str = ""
    busy: bool = False
    rates: list[int] = field(default_factory=list)
    formats: list[str] = field(default_factory=list)
    ch_min: int = 0
    ch_max: int = 0


def _open_pcm(device: str):
    pcm = ctypes.c_void_p()
    ret = lib().snd_pcm_open(ctypes.byref(pcm), device.encode(), SND_PCM_STREAM_PLAYBACK, 0)
    return ret, pcm


def probe_caps(device: str) -> Caps:
    L = lib()
    ret, pcm = _open_pcm(device)
    if ret < 0:
        return Caps(device, False, strerror(ret), busy=(-ret == errno.EBUSY))
    hw = ctypes.c_void_p()
    try:
        L.snd_pcm_hw_params_malloc(ctypes.byref(hw))
        if L.snd_pcm_hw_params_any(pcm, hw) < 0:
            return Caps(device, False, "hw_params_any failed")
        rates = [r for r in STD_RATES if L.snd_pcm_hw_params_test_rate(pcm, hw, r, 0) == 0]
        fmts = [n for n, e in ALSA_FORMAT_ENUM.items() if L.snd_pcm_hw_params_test_format(pcm, hw, e) == 0]
        cmin, cmax = ctypes.c_uint(), ctypes.c_uint()
        L.snd_pcm_hw_params_get_channels_min(hw, ctypes.byref(cmin))
        L.snd_pcm_hw_params_get_channels_max(hw, ctypes.byref(cmax))
        return Caps(device, True, "", False, rates, fmts, int(cmin.value), int(cmax.value))
    finally:
        if hw:
            L.snd_pcm_hw_params_free(hw)
        L.snd_pcm_close(pcm)


def probe_format(device: str, rate: int, channels: int, candidates: list[str] | None = None) -> tuple[str | None, str]:
    """(rate, channels) の組で受け付けられる最良の形式を返す。(形式 or None, エラー文字列)"""
    L = lib()
    ret, pcm = _open_pcm(device)
    if ret < 0:
        return None, ("busy" if -ret == errno.EBUSY else strerror(ret))
    hw = ctypes.c_void_p()
    try:
        L.snd_pcm_hw_params_malloc(ctypes.byref(hw))
        for fmt in candidates or FORMAT_PREFERENCE:
            L.snd_pcm_hw_params_any(pcm, hw)
            L.snd_pcm_hw_params_set_rate_resample(pcm, hw, 0)
            if L.snd_pcm_hw_params_set_access(pcm, hw, SND_PCM_ACCESS_RW_INTERLEAVED) < 0:
                continue
            if L.snd_pcm_hw_params_set_channels(pcm, hw, channels) < 0:
                return None, "channels"
            if L.snd_pcm_hw_params_set_rate(pcm, hw, rate, 0) < 0:
                return None, "rate"
            if L.snd_pcm_hw_params_set_format(pcm, hw, ALSA_FORMAT_ENUM[fmt]) < 0:
                continue
            return fmt, ""
        return None, "format"
    finally:
        if hw:
            L.snd_pcm_hw_params_free(hw)
        L.snd_pcm_close(pcm)


# ---------------------------------------------------------------- 再生ハンドル
class AlsaPcm:
    """開いた PCM。write() はブロッキング。"""

    def __init__(self):
        self.pcm = ctypes.c_void_p()
        self.period_frames = 0
        self.buffer_frames = 0
        self.frame_bytes = 0
        self.xruns = 0
        self.device = ""

    def open(self, device: str, rate: int, channels: int, fmt: str, frame_bytes: int,
             period_ms: float, periods: int, allow_resample: bool) -> None:
        L = lib()
        self.device = device
        self.frame_bytes = frame_bytes
        ret = L.snd_pcm_open(ctypes.byref(self.pcm), device.encode(), SND_PCM_STREAM_PLAYBACK, 0)
        if ret < 0:
            raise AlsaError(-ret, f"snd_pcm_open({device}): {strerror(ret)}")
        hw = ctypes.c_void_p()
        sw = ctypes.c_void_p()
        try:
            _chk(L.snd_pcm_hw_params_malloc(ctypes.byref(hw)), "hw_params_malloc")
            _chk(L.snd_pcm_hw_params_any(self.pcm, hw), "hw_params_any")
            L.snd_pcm_hw_params_set_rate_resample(self.pcm, hw, 1 if allow_resample else 0)
            _chk(L.snd_pcm_hw_params_set_access(self.pcm, hw, SND_PCM_ACCESS_RW_INTERLEAVED), "set_access")
            _chk(L.snd_pcm_hw_params_set_format(self.pcm, hw, ALSA_FORMAT_ENUM[fmt]), f"set_format({fmt})")
            _chk(L.snd_pcm_hw_params_set_channels(self.pcm, hw, channels), f"set_channels({channels})")
            _chk(L.snd_pcm_hw_params_set_rate(self.pcm, hw, rate, 0), f"set_rate({rate})")
            period_us = ctypes.c_uint(int(period_ms * 1000))
            buffer_us = ctypes.c_uint(int(period_ms * 1000 * max(2, periods)))
            d = ctypes.c_int(0)
            L.snd_pcm_hw_params_set_buffer_time_near(self.pcm, hw, ctypes.byref(buffer_us), ctypes.byref(d))
            L.snd_pcm_hw_params_set_period_time_near(self.pcm, hw, ctypes.byref(period_us), ctypes.byref(d))
            _chk(L.snd_pcm_hw_params(self.pcm, hw), "hw_params")
            pf, bf = ctypes.c_ulong(), ctypes.c_ulong()
            L.snd_pcm_hw_params_get_period_size(hw, ctypes.byref(pf), ctypes.byref(d))
            L.snd_pcm_hw_params_get_buffer_size(hw, ctypes.byref(bf))
            self.period_frames, self.buffer_frames = int(pf.value), int(bf.value)
            _chk(L.snd_pcm_sw_params_malloc(ctypes.byref(sw)), "sw_params_malloc")
            _chk(L.snd_pcm_sw_params_current(self.pcm, sw), "sw_params_current")
            # バッファを満たしてから開始 → 開始直後のアンダーランを避ける
            L.snd_pcm_sw_params_set_start_threshold(self.pcm, sw, self.buffer_frames)
            L.snd_pcm_sw_params_set_avail_min(self.pcm, sw, self.period_frames)
            _chk(L.snd_pcm_sw_params(self.pcm, sw), "sw_params")
            _chk(L.snd_pcm_prepare(self.pcm), "prepare")
        except Exception:
            self.close(drain=False)
            raise
        finally:
            if hw:
                L.snd_pcm_hw_params_free(hw)
            if sw:
                L.snd_pcm_sw_params_free(sw)

    def write(self, buf, frames: int) -> None:
        """buf: ctypes に渡せる連続バッファ(numpy uint8 配列)。frames フレームを全て書く。"""
        L = lib()
        addr = buf.ctypes.data
        left = frames
        fb = self.frame_bytes
        tries = 0
        while left > 0:
            n = L.snd_pcm_writei(self.pcm, addr, left)
            if n < 0:
                tries += 1
                if -n == errno.EPIPE:
                    self.xruns += 1
                if tries > 5:
                    raise AlsaError(-n, f"snd_pcm_writei: {strerror(int(n))}")
                r = L.snd_pcm_recover(self.pcm, int(n), 1)
                if r < 0:
                    raise AlsaError(-r, f"snd_pcm_recover: {strerror(r)}")
                continue
            tries = 0
            left -= n
            addr += n * fb

    def delay(self) -> int:
        d = ctypes.c_long(0)
        if lib().snd_pcm_delay(self.pcm, ctypes.byref(d)) < 0:
            return 0
        return max(0, int(d.value))

    def close(self, drain: bool = True) -> None:
        if not self.pcm:
            return
        L = lib()
        try:
            if drain:
                L.snd_pcm_drain(self.pcm)
            else:
                L.snd_pcm_drop(self.pcm)
        finally:
            L.snd_pcm_close(self.pcm)
            self.pcm = ctypes.c_void_p()
