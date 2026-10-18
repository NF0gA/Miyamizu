"""共有メモリ上のリングバッファ(単一生産者/単一消費者)。

ヘッダ(256B):
  int64[0] write_pos   生産者だけが書く。累積フレーム数
  int64[1] read_pos    消費者だけが書く。累積フレーム数
  int64[2] done        生産者: 1 ならこれ以上書かれない
  int64[3] error       生産者: 1 ならデコード失敗
  int64[4] capacity    フレーム数
  int64[5] ring        1=循環(ストリーミング) / 0=線形(RAM 再生。read は write に追いつくだけ)
  float64@64[0] volume 消費者(エンジン)が書き、生産者が読む
x86-64 では整数1個の書き込みは分割されず順序も保たれるので、ロック無しで安全に使える。
"""
from __future__ import annotations

import itertools
import os
from multiprocessing import shared_memory

import numpy as np

HEADER_BYTES = 256
_counter = itertools.count()


def _attach_shm(name: str) -> shared_memory.SharedMemory:
    try:
        return shared_memory.SharedMemory(name=name, create=False, track=False)  # Python 3.13+
    except TypeError:
        shm = shared_memory.SharedMemory(name=name, create=False)
        try:  # 生産者側の終了で消されないようにトラッカーから外す
            from multiprocessing import resource_tracker

            resource_tracker.unregister(shm._name, "shared_memory")  # type: ignore[attr-defined]
        except Exception:  # noqa: BLE001
            pass
        return shm


class SharedSource:
    def __init__(self, shm: shared_memory.SharedMemory, frame_bytes: int, owner: bool):
        self.shm = shm
        self.frame_bytes = frame_bytes
        self.owner = owner
        self._hdr = np.ndarray((8,), dtype=np.int64, buffer=shm.buf, offset=0)
        self._hf = np.ndarray((8,), dtype=np.float64, buffer=shm.buf, offset=64)
        self.capacity = int(self._hdr[4])
        self.ring = bool(self._hdr[5])
        self.data = np.ndarray((self.capacity * frame_bytes,), dtype=np.uint8, buffer=shm.buf, offset=HEADER_BYTES)

    # ---- 生成/接続 ----
    @classmethod
    def create(cls, capacity_frames: int, frame_bytes: int, ring: bool) -> "SharedSource":
        name = f"miyamizu-{os.getpid()}-{next(_counter)}"
        size = HEADER_BYTES + capacity_frames * frame_bytes
        shm = shared_memory.SharedMemory(name=name, create=True, size=size)
        hdr = np.ndarray((8,), dtype=np.int64, buffer=shm.buf, offset=0)
        hdr[:] = 0
        hdr[4] = capacity_frames
        hdr[5] = 1 if ring else 0
        hf = np.ndarray((8,), dtype=np.float64, buffer=shm.buf, offset=64)
        hf[0] = 1.0
        del hdr, hf
        return cls(shm, frame_bytes, owner=True)

    @classmethod
    def attach(cls, name: str, frame_bytes: int) -> "SharedSource":
        return cls(_attach_shm(name), frame_bytes, owner=False)

    @property
    def name(self) -> str:
        return self.shm.name

    # ---- 状態 ----
    @property
    def write_pos(self) -> int:
        return int(self._hdr[0])

    @property
    def read_pos(self) -> int:
        return int(self._hdr[1])

    @read_pos.setter
    def read_pos(self, v: int) -> None:
        self._hdr[1] = v

    @property
    def done(self) -> bool:
        return bool(self._hdr[2])

    @property
    def error(self) -> bool:
        return bool(self._hdr[3])

    @property
    def volume(self) -> float:
        return float(self._hf[0])

    @volume.setter
    def volume(self, v: float) -> None:
        self._hf[0] = v

    def available(self) -> int:
        return int(self._hdr[0]) - int(self._hdr[1])

    def free(self) -> int:
        if self.ring:
            return self.capacity - (int(self._hdr[0]) - int(self._hdr[1]))
        return self.capacity - int(self._hdr[0])

    def finished(self) -> bool:
        """全て読み終えた(あるいは失敗して空)。"""
        return bool(self._hdr[2]) and int(self._hdr[0]) - int(self._hdr[1]) <= 0

    def mark_done(self, error: bool = False) -> None:
        if error:
            self._hdr[3] = 1
        self._hdr[2] = 1

    # ---- 生産者 ----
    def write(self, buf: np.ndarray) -> int:
        """buf: uint8 フラット(フレーム境界)。書けただけ書き、書いたフレーム数を返す。"""
        fb = self.frame_bytes
        n = min(buf.shape[0] // fb, self.free())
        if n <= 0:
            return 0
        wp = int(self._hdr[0])
        if self.ring:
            start = wp % self.capacity
            first = min(n, self.capacity - start)
            self.data[start * fb:(start + first) * fb] = buf[:first * fb]
            if first < n:
                self.data[:(n - first) * fb] = buf[first * fb:n * fb]
        else:
            self.data[wp * fb:(wp + n) * fb] = buf[:n * fb]
        self._hdr[0] = wp + n   # 最後に公開する
        return n

    # ---- 消費者 ----
    def read_into(self, out: np.ndarray, out_off_frames: int, max_frames: int) -> int:
        """out(uint8 フラット)の out_off_frames から最大 max_frames を読み込み、フレーム数を返す。"""
        fb = self.frame_bytes
        rp = int(self._hdr[1])
        n = min(max_frames, int(self._hdr[0]) - rp)
        if n <= 0:
            return 0
        if self.ring:
            start = rp % self.capacity
            first = min(n, self.capacity - start)
            o = out_off_frames * fb
            out[o:o + first * fb] = self.data[start * fb:(start + first) * fb]
            if first < n:
                out[o + first * fb:o + n * fb] = self.data[:(n - first) * fb]
        else:
            o = out_off_frames * fb
            out[o:o + n * fb] = self.data[rp * fb:(rp + n) * fb]
        self._hdr[1] = rp + n
        return n

    # ---- 後始末 ----
    def close(self) -> None:
        self._hdr = None
        self._hf = None
        self.data = None
        try:
            self.shm.close()
        except BufferError:
            pass

    def unlink(self) -> None:
        if self.owner:
            try:
                self.shm.unlink()
            except FileNotFoundError:
                pass
