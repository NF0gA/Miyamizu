"""Windows 用の出力(WASAPI)。sounddevice(PortAudio)経由で、alsa.py と同じ関数名で提供する。

- 排他(kind="alsa_hw"): WASAPI 排他モード。実際に開ける形式(整数 32/24/16bit)を問い合わせて、変換なしで渡す。
- 共有(kind="pipewire"/"custom"): WASAPI 共有。OS のミキサー形式へは PortAudio(auto_convert)が変換する。
実機(Windows)での動作は未検証。ここでの関数は engine.py が alsa.* として呼ぶ(list_hw_devices / probe_caps / probe_format / usb_stream_info)。
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from .alsa import Caps
from .common import STD_RATES

DTYPE = {"S32_LE": "int32", "S24_3LE": "int24", "S16_LE": "int16"}     # PortAudio の形式。24bit 有効の S32 は左詰めのまま渡す
PREFERENCE = ["S32_LE", "S24_3LE", "S16_LE"]


class WasapiError(OSError):
    pass


def _sd():
    import sounddevice as sd      # 遅延 import(未導入なら呼び出し側で EngineError になる)
    return sd


@dataclass
class AlsaDevice:                 # alsa.AlsaDevice と同じ形(GUI が name/label/card_index を使う)
    name: str
    label: str
    card_index: int
    card_id: str = ""


def _wasapi_index() -> int | None:
    sd = _sd()
    for i, h in enumerate(sd.query_hostapis()):
        if "WASAPI" in h["name"]:
            return i
    return None


def list_hw_devices() -> list[AlsaDevice]:
    """WASAPI の出力デバイス。"""
    try:
        sd = _sd()
        hi = _wasapi_index()
        if hi is None:
            return []
        out = []
        for idx in sd.query_hostapis(hi)["devices"]:
            d = sd.query_devices(idx)
            if d["max_output_channels"] > 0:
                out.append(AlsaDevice(d["name"], f"{d['name']} (WASAPI)", int(idx), str(idx)))
        return out
    except Exception:  # noqa: BLE001
        return []


def usb_stream_info(card_index: int) -> str:
    return ""


def resolve(device: str | None) -> int | None:
    """デバイス名("default" や空は既定の出力) → PortAudio のデバイス番号。"""
    sd = _sd()
    hi = _wasapi_index()
    if hi is None:
        return None
    if device and device not in ("default", "pipewire"):
        for d in list_hw_devices():
            if d.name == device:
                return d.card_index
    idx = sd.query_hostapis(hi).get("default_output_device", -1)
    return idx if idx is not None and idx >= 0 else None


def _settings(exclusive: bool, explicit: bool = True):
    sd = _sd()
    if exclusive:
        try:
            return sd.WasapiSettings(exclusive=True, explicit_sample_format=explicit)
        except TypeError:                               # 古い sounddevice
            return sd.WasapiSettings(exclusive=True)
    try:
        return sd.WasapiSettings(exclusive=False, auto_convert=True)
    except TypeError:
        return sd.WasapiSettings(exclusive=False)


def _check(idx: int, rate: int, ch: int, fmt: str, exclusive: bool) -> tuple[bool, str]:
    sd = _sd()
    try:
        sd.check_output_settings(device=idx, channels=ch, dtype=DTYPE[fmt], samplerate=rate,
                                 extra_settings=_settings(exclusive))
        return True, ""
    except Exception as e:  # noqa: BLE001
        return False, str(e)


def probe_caps(device: str) -> Caps:
    """排他モードで開ける レート/形式/チャンネル数。"""
    try:
        idx = resolve(device)
    except Exception as e:  # noqa: BLE001
        return Caps(device, False, f"sounddevice を使えません: {e}")
    if idx is None:
        return Caps(device, False, "WASAPI デバイスが見つかりません")
    sd = _sd()
    info = sd.query_devices(idx)
    maxch = int(info["max_output_channels"])
    ch = 2 if maxch >= 2 else 1
    rates = [r for r in STD_RATES if any(_check(idx, r, ch, f, True)[0] for f in PREFERENCE)]
    fmts = [f for f in PREFERENCE if any(_check(idx, r, ch, f, True)[0] for r in (rates or [44100]))]
    if not rates:
        ok, err = _check(idx, 44100, ch, "S16_LE", True)
        busy = "in use" in err.lower() or "unavailable" in err.lower() or "exclusive" in err.lower()
        return Caps(device, False, err or "排他モードで開けません", busy=busy)
    return Caps(device, True, "", False, rates, fmts, 1, maxch)


def probe_format(device: str, rate: int, channels: int, candidates: list[str] | None = None) -> tuple[str | None, str]:
    idx = resolve(device)
    if idx is None:
        return None, "device"
    last = ""
    for f in candidates or PREFERENCE:
        if f not in DTYPE:
            continue
        ok, err = _check(idx, rate, channels, f, True)
        if ok:
            return f, ""
        last = err
    return None, last or "format"


class WasapiPcm:
    """開いたストリーム。write() はブロッキング(PortAudio のイベント駆動バッファ)。"""

    def __init__(self):
        self.stream = None
        self.period_frames = 0
        self.buffer_frames = 0
        self.frame_bytes = 0
        self.xruns = 0
        self.rate = 44100

    def open(self, device: str, rate: int, channels: int, fmt: str, frame_bytes: int, period_ms: float, periods: int,
             exclusive: bool) -> None:
        sd = _sd()
        idx = resolve(device)
        if idx is None:
            raise WasapiError("WASAPI デバイスが見つかりません")
        if fmt not in DTYPE:
            raise WasapiError(f"WASAPI で扱えない形式: {fmt}")
        self.frame_bytes, self.rate = frame_bytes, rate
        self.period_frames = max(32, int(rate * period_ms / 1000))
        self.buffer_frames = self.period_frames * max(2, periods)
        try:
            self.stream = sd.RawOutputStream(samplerate=rate, blocksize=self.period_frames, device=idx, channels=channels,
                                             dtype=DTYPE[fmt], latency=self.buffer_frames / rate,
                                             extra_settings=_settings(exclusive))
            self.stream.start()
        except Exception as e:  # noqa: BLE001
            self.stream = None
            msg = str(e)
            low = msg.lower()
            raise WasapiError(f"WASAPI を開けません({'排他' if exclusive else '共有'}): {msg}" +
                              (" busy" if ("in use" in low or "unavailable" in low or "exclusive" in low) else "")) from e

    def write(self, buf, frames: int) -> None:
        under = self.stream.write(memoryview(np.ascontiguousarray(buf[: frames * self.frame_bytes])))
        if under:
            self.xruns += 1

    def delay(self) -> int:
        try:
            return int(self.stream.latency * self.rate)
        except Exception:  # noqa: BLE001
            return 0

    def close(self, drain: bool = True) -> None:
        st, self.stream = self.stream, None
        if st is None:
            return
        try:
            (st.stop if drain else st.abort)()
        except Exception:  # noqa: BLE001
            pass
        try:
            st.close()
        except Exception:  # noqa: BLE001
            pass
