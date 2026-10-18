"""デコーダ。soundfile(libsndfile) と PyAV(ffmpeg) を同じインターフェースで扱う。

read(n) は次のどちらかを返す:
  * is_int=True  : int32 (frames, ch)  最上位に揃えた整数 (PCM 系。ビットパーフェクト可)
  * is_int=False : float64 (frames, ch) (非可逆圧縮など)
範囲 [start_frame, end_frame) だけを読む (CUE トラック用)。
"""
from __future__ import annotations

from pathlib import Path

import numpy as np

from .common import SF_EXTS

_PCM_BITS = {"PCM_S8": 8, "PCM_U8": 8, "PCM_16": 16, "PCM_24": 24, "PCM_32": 32}


class DecodeError(Exception):
    pass


def _use_av(path: str) -> bool:
    if path.startswith(("http://", "https://")):
        return True
    return Path(path).suffix.lower() not in SF_EXTS


def probe(path: str, av_options: dict | None = None) -> dict:
    """ファイルの基本情報を返す: rate, channels, frames, is_int, src_bits, codec"""
    if not _use_av(path):
        import soundfile as sf

        try:
            info = sf.info(path)
        except Exception as e:  # noqa: BLE001
            raise DecodeError(f"{path}: {e}") from e
        bits = _PCM_BITS.get(info.subtype)
        return {
            "rate": int(info.samplerate),
            "channels": int(info.channels),
            "frames": int(info.frames),
            "is_int": bits is not None,
            "src_bits": bits or 32,
            "codec": f"{info.format}/{info.subtype}",
        }
    d = AvDecoder(path, av_options=av_options)
    try:
        return {
            "rate": d.rate,
            "channels": d.channels,
            "frames": d.total_frames,
            "is_int": d.is_int,
            "src_bits": d.src_bits,
            "codec": d.codec_name,
        }
    finally:
        d.close()


class SoundFileDecoder:
    def __init__(self, path: str, start_frame: int = 0, end_frame: int | None = None):
        import soundfile as sf

        try:
            self.f = sf.SoundFile(path)
        except Exception as e:  # noqa: BLE001
            raise DecodeError(f"{path}: {e}") from e
        self.rate = int(self.f.samplerate)
        self.channels = int(self.f.channels)
        self.total_frames = int(self.f.frames)
        bits = _PCM_BITS.get(self.f.subtype)
        self.is_int = bits is not None
        self.src_bits = bits or 32
        self.codec_name = f"{self.f.format}/{self.f.subtype}"
        self.start = max(0, int(start_frame))
        self.end = self.total_frames if end_frame is None else min(int(end_frame), self.total_frames)
        self.remaining = max(0, self.end - self.start)
        if self.start:
            self.f.seek(self.start)

    def read(self, n: int) -> np.ndarray:
        n = min(n, self.remaining)
        if n <= 0:
            return np.zeros((0, self.channels), dtype=np.int32 if self.is_int else np.float64)
        dtype = "int32" if self.is_int else "float64"
        blk = self.f.read(n, dtype=dtype, always_2d=True)
        self.remaining -= blk.shape[0]
        if blk.shape[0] == 0:
            self.remaining = 0
        return blk

    def close(self):
        try:
            self.f.close()
        except Exception:  # noqa: BLE001
            pass


class AvDecoder:
    """m4a / aac / ALAC など。PyAV でデコードし、整数(ALAC)または float64 で返す。"""

    def __init__(self, path: str, start_frame: int = 0, end_frame: int | None = None, av_options: dict | None = None):
        import av

        self._av = av
        try:
            self.c = av.open(path, options=av_options or None, timeout=(15.0, 15.0) if av_options else None)
            self.s = self.c.streams.audio[0]
        except Exception as e:  # noqa: BLE001
            raise DecodeError(f"{path}: {e}") from e
        cc = self.s.codec_context
        self.rate = int(cc.sample_rate or self.s.rate)
        self.channels = self._channels(cc)
        self.codec_name = cc.name
        self.is_int = cc.name in ("alac", "wavpack", "ape")
        bits = getattr(cc, "bits_per_coded_sample", 0) or getattr(cc, "bits_per_raw_sample", 0)
        self.src_bits = int(bits) if (self.is_int and bits) else (24 if self.is_int else 32)
        if self.s.duration is not None and self.s.time_base is not None:
            self.total_frames = int(round(float(self.s.duration * self.s.time_base) * self.rate))
        elif self.c.duration:
            self.total_frames = int(round(self.c.duration / 1_000_000 * self.rate))
        else:
            self.total_frames = 0
        self.start = max(0, int(start_frame))
        self.end = self.total_frames if end_frame is None else (min(int(end_frame), self.total_frames) if self.total_frames else int(end_frame))
        self.remaining = max(0, self.end - self.start) if self.end else 1 << 62
        self._buf: list[np.ndarray] = []
        self._buf_frames = 0
        self._eof = False
        self._skip = 0
        self._first = True
        self._layout = None
        self._rs = None
        fmt = "s32" if self.is_int else "dbl"
        self._fmt = fmt
        self._frames_iter = None
        if self.start:
            self._seek(self.start)
        self._frames_iter = self.c.decode(self.s)

    @staticmethod
    def _channels(cc) -> int:
        for attr in ("channels",):
            v = getattr(cc, attr, None)
            if v:
                return int(v)
        layout = getattr(cc, "layout", None)
        if layout is not None:
            try:
                return len(layout.channels)
            except Exception:  # noqa: BLE001
                pass
        return 2

    def _seek(self, frame: int):
        tb = self.s.time_base
        ts = int(frame / self.rate / float(tb))
        try:
            self.c.seek(max(0, ts), stream=self.s, backward=True, any_frame=False)
        except Exception:  # noqa: BLE001
            self.c.seek(0)
        self._seek_target = frame
        self._skip = None  # 最初のフレームの pts から計算する

    def _convert(self, frame) -> np.ndarray:
        if self._rs is None:
            self._rs = self._av.AudioResampler(format=self._fmt, layout=frame.layout.name, rate=self.rate)
        out = self._rs.resample(frame)
        if out is None:
            return np.zeros((0, self.channels), dtype=np.int32 if self.is_int else np.float64)
        if not isinstance(out, (list, tuple)):
            out = [out]
        parts = []
        for fr in out:
            a = fr.to_ndarray().reshape(-1, self.channels)
            parts.append(a)
        if not parts:
            return np.zeros((0, self.channels), dtype=np.int32 if self.is_int else np.float64)
        a = np.concatenate(parts, axis=0)
        return a.astype(np.int32 if self.is_int else np.float64, copy=False)

    def _fill(self, n: int):
        while self._buf_frames < n and not self._eof:
            try:
                frame = next(self._frames_iter)
            except StopIteration:
                self._eof = True
                break
            except Exception:  # noqa: BLE001
                self._eof = True
                break
            if self._skip is None:
                pts = frame.pts
                if pts is not None:
                    first = int(round(float(pts * self.s.time_base) * self.rate))
                    self._skip = max(0, self._seek_target - first)
                else:
                    self._skip = 0
            a = self._convert(frame)
            if self._skip:
                drop = min(self._skip, a.shape[0])
                a = a[drop:]
                self._skip -= drop
            if a.shape[0]:
                self._buf.append(a)
                self._buf_frames += a.shape[0]

    def read(self, n: int) -> np.ndarray:
        n = min(n, self.remaining)
        empty = np.zeros((0, self.channels), dtype=np.int32 if self.is_int else np.float64)
        if n <= 0:
            return empty
        if self._skip is None and not self._buf:
            self._skip = None
        self._fill(n)
        if not self._buf:
            self.remaining = 0
            return empty
        a = np.concatenate(self._buf, axis=0) if len(self._buf) > 1 else self._buf[0]
        out, rest = a[:n], a[n:]
        self._buf = [rest] if rest.shape[0] else []
        self._buf_frames = rest.shape[0]
        self.remaining -= out.shape[0]
        return out

    def close(self):
        try:
            self.c.close()
        except Exception:  # noqa: BLE001
            pass


def open_decoder(path: str, start_frame: int = 0, end_frame: int | None = None, av_options: dict | None = None):
    if _use_av(path):
        return AvDecoder(path, start_frame, end_frame, av_options=av_options)
    return SoundFileDecoder(path, start_frame, end_frame)
