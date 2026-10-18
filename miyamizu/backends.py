"""出力バックエンド。AlsaBackend が本番、Null/Capture はテストとヘッドレス確認用。"""
from __future__ import annotations

import time
from dataclasses import dataclass

import numpy as np


@dataclass(frozen=True)
class SessionParams:
    kind: str            # pipewire | alsa_hw | custom | test
    device: str
    rate: int
    channels: int
    fmt: str
    valid_bits: int
    frame_bytes: int
    period_ms: float
    periods: int


class Backend:
    period_frames = 0
    buffer_frames = 0

    def open(self, p: SessionParams) -> None: ...
    def write(self, buf: np.ndarray, frames: int) -> None: ...
    def delay(self) -> int: return 0
    def xruns(self) -> int: return 0
    def close(self, drain: bool = True) -> None: ...


class AlsaBackend(Backend):
    def __init__(self):
        from .alsa import AlsaPcm

        self.pcm = AlsaPcm()

    def open(self, p: SessionParams) -> None:
        self.pcm.open(p.device, p.rate, p.channels, p.fmt, p.frame_bytes, p.period_ms, p.periods,
                      allow_resample=(p.kind != "alsa_hw"))
        self.period_frames = self.pcm.period_frames
        self.buffer_frames = self.pcm.buffer_frames

    def write(self, buf, frames):
        self.pcm.write(buf, frames)

    def delay(self):
        return self.pcm.delay()

    def xruns(self):
        return self.pcm.xruns

    def close(self, drain=True):
        self.pcm.close(drain)


class WasapiBackend(Backend):
    """Windows。kind="alsa_hw" は WASAPI 排他(ビットパーフェクト)、それ以外は共有。"""

    def __init__(self):
        from .wasapi import WasapiPcm

        self.pcm = WasapiPcm()

    def open(self, p: SessionParams) -> None:
        self.pcm.open(p.device, p.rate, p.channels, p.fmt, p.frame_bytes, p.period_ms, p.periods,
                      exclusive=(p.kind == "alsa_hw"))
        self.period_frames = self.pcm.period_frames
        self.buffer_frames = self.pcm.buffer_frames

    def write(self, buf, frames):
        self.pcm.write(buf, frames)

    def delay(self):
        return self.pcm.delay()

    def xruns(self):
        return self.pcm.xruns

    def close(self, drain=True):
        self.pcm.close(drain)


class NullBackend(Backend):
    """デバイスなし。絶対時刻基準でペーシングする(累積誤差なし)。paced=False なら待たない。"""

    def __init__(self, paced: bool = True):
        self.paced = paced
        self.rate = 44100
        self.written = 0
        self.t0 = 0.0

    def open(self, p: SessionParams) -> None:
        self.rate = p.rate
        self.period_frames = max(16, int(p.rate * p.period_ms / 1000))
        self.buffer_frames = self.period_frames * max(2, p.periods)
        self.written = 0
        self.t0 = time.perf_counter()
        self.frame_bytes = p.frame_bytes

    def _consumed(self) -> int:
        return int((time.perf_counter() - self.t0) * self.rate)

    def write(self, buf, frames):
        self.written += frames
        if self.paced:
            wake = self.t0 + (self.written - self.buffer_frames) / self.rate
            dt = wake - time.perf_counter()
            if dt > 0:
                time.sleep(dt)

    def delay(self):
        if not self.paced:
            return 0
        return max(0, min(self.buffer_frames, self.written - self._consumed()))


class CaptureBackend(NullBackend):
    """出力バイト列を丸ごと保持する(ビットパーフェクト検証用)。"""

    def __init__(self, paced: bool = False, path: str | None = None):
        super().__init__(paced)
        self.path = path
        self.chunks: list[bytes] = []
        self.sessions: list[SessionParams] = []

    def open(self, p):
        super().open(p)
        self.sessions.append(p)

    def write(self, buf, frames):
        self.chunks.append(bytes(buf[: frames * self.frame_bytes]))
        super().write(buf, frames)

    def close(self, drain=True):
        if self.path and self.chunks:
            with open(self.path, "ab") as f:
                f.write(b"".join(self.chunks))
            self.chunks = []


def make_backend(test_backend: str | None) -> Backend:
    if not test_backend:
        import sys
        if sys.platform == "win32":
            return WasapiBackend()
        return AlsaBackend()
    if test_backend == "null":
        return NullBackend(paced=True)
    if test_backend.startswith("capture-paced"):
        return CaptureBackend(paced=True)
    if test_backend.startswith("capture"):
        path = test_backend.split(":", 1)[1] if ":" in test_backend else None
        return CaptureBackend(paced=False, path=path)
    raise ValueError(test_backend)
