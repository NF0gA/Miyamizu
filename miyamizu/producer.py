"""準備プロセス: デコード → (チャンネル整形) → (音量) → (リサンプル) → デバイス形式への変換 → 共有メモリ。

出力スレッドとは別プロセス・別コアで動く。出力スレッドは共有メモリから読むだけ。
"""
from __future__ import annotations

import math
import os
import time
import traceback
from collections import OrderedDict

import numpy as np

from . import convert
from .decode import DecodeError, open_decoder
from .shm_ring import SharedSource


def map_channels(x: np.ndarray, out_ch: int) -> np.ndarray:
    ch = x.shape[1]
    if ch == out_ch:
        return x
    if ch == 1:
        return np.repeat(x, out_ch, axis=1)
    if out_ch == 1:
        if x.dtype.kind == "f":
            return x.mean(axis=1, keepdims=True)
        return (x.astype(np.int64).sum(axis=1, keepdims=True) // ch).astype(x.dtype)
    if out_ch < ch:
        return np.ascontiguousarray(x[:, :out_ch])
    pad = np.zeros((x.shape[0], out_ch - ch), dtype=x.dtype)
    return np.concatenate([x, pad], axis=1)


def make_resampler(src_rate: int, out_rate: int, ch: int, quality: str, phase: str, band: str):
    """標準設定は python-soxr と同一出力の libsoxr(ctypes)、位相/帯域を指定した時も libsoxr。
    libsoxr が無ければ python-soxr(位相・帯域は選べない)にフォールバックする。"""
    from . import soxr_ctypes

    if soxr_ctypes.available():
        try:
            return soxr_ctypes.SoxrStream(src_rate, out_rate, ch, quality, phase, steep=(band == "wide"))
        except RuntimeError:
            pass
    import soxr

    q = quality if quality in ("QQ", "LQ", "MQ", "HQ", "VHQ") else "VHQ"
    return soxr.ResampleStream(src_rate, out_rate, ch, quality=q, dtype="float64")


class Job:
    def __init__(self, p: dict):
        self.p = p
        self.prio = int(p.get("prio", 0))      # 0: 再生中/次曲  1: 事前変換(先行)。小さいほど先に処理する
        self.id = p["job_id"]
        self.src = SharedSource.attach(p["shm_name"], p["frame_bytes"])
        self.fb = p["frame_bytes"]
        self.dec = open_decoder(p["path"], p.get("start_frame", 0), p.get("end_frame"), p.get("av_options"))
        self.src_rate = self.dec.rate
        self.out_rate = p["out_rate"]
        self.out_ch = p["out_channels"]
        self.fmt = p["fmt"]
        self.valid_bits = p["valid_bits"]
        self.dither = bool(p.get("dither", True))
        self.rng = np.random.default_rng(p.get("seed", 1))
        self.shaper = _make_shaper(p)
        self.ratio = self.out_rate / self.src_rate
        self.rs = None
        if self.out_rate != self.src_rate:
            self.rs = None
            if p.get("engine") == "r8brain":
                from . import r8brain
                if r8brain.available():
                    try:
                        self.rs = r8brain.R8Stream(self.src_rate, self.out_rate, self.out_ch, float(p.get("r8_tb", 2.0)),
                                                   float(p.get("r8_att", 206.91)), p.get("r8_phase", "linear"))
                    except RuntimeError:
                        self.rs = None
            if self.rs is None:
                self.rs = make_resampler(self.src_rate, self.out_rate, self.out_ch, p.get("quality", "VHQ"),
                                         p.get("phase", "linear"), p.get("band", "standard"))
        self.headroom = 10 ** (min(0.0, float(p.get("r8_headroom_db", 0.0))) / 20) if self.rs is not None else 1.0
        self.chunk_in = int(p.get("chunk_in", 4096))
        self.out_chunk = int(math.ceil(self.chunk_in * self.ratio)) + 64
        self.pending: np.ndarray | None = None
        self.eof = False
        self.closed = False
        self.reported = False
        self.error: str | None = None

    # ---- 1ブロック処理 ----
    def _process(self, block: np.ndarray, last: bool) -> np.ndarray:
        is_int = self.dec.is_int
        vol = self.src.volume
        block = map_channels(block, self.out_ch) if block.shape[0] else np.zeros((0, self.out_ch), dtype=block.dtype)
        if self.rs is None and vol == 1.0 and is_int:
            y = convert.requantize_left_i32(block, self.valid_bits, self.dither, self.rng, self.shaper)
        else:
            x = convert.i32_to_float(block) if is_int else block
            if vol != 1.0:
                x = x * vol
            if self.rs is not None:
                if self.headroom != 1.0:
                    x = x * self.headroom
                x = self.rs.resample_chunk(np.ascontiguousarray(x), last=last)
            y = (self.shaper.quantize(x) if self.shaper is not None and self.valid_bits < 32
                 else convert.float_to_left_i32(x, self.valid_bits, self.dither, self.rng))
        return convert.pack(y, self.fmt)

    def step(self) -> bool:
        """1チャンク分だけ仕事をする。何かしたら True。"""
        if self.closed or self.src.done:
            return False
        try:
            if self.pending is not None:
                n = self.src.write(self.pending)
                if n == 0:
                    if not self.src.ring:
                        raise RuntimeError("RAMバッファ溢れ(リサンプラの出力が見積りより多い)")
                    return False
                rest = self.pending[n * self.fb:]
                self.pending = rest if rest.shape[0] else None
                if self.pending is None and self.eof:
                    self.src.mark_done()
                return True
            if self.eof:
                self.src.mark_done()
                return True
            if self.src.ring and self.src.free() < self.out_chunk:
                return False
            block = self.dec.read(self.chunk_in)
            last = block.shape[0] == 0
            if last:
                self.eof = True
                if self.rs is not None:
                    out = self._process(np.zeros((0, self.dec.channels), dtype=block.dtype), True)
                else:
                    out = np.zeros(0, dtype=np.uint8)
            else:
                out = self._process(block, False)
            if out.shape[0]:
                n = self.src.write(out)
                rest = out[n * self.fb:]
                self.pending = rest if rest.shape[0] else None
            if self.eof and self.pending is None:
                self.src.mark_done()
            return True
        except (DecodeError, Exception) as e:  # noqa: BLE001
            self.error = f"{type(e).__name__}: {e}"
            self.src.mark_done(error=True)
            return True

    def close(self):
        if self.closed:
            return
        self.closed = True
        try:
            self.dec.close()
        except Exception:  # noqa: BLE001
            pass
        self.rs = None
        try:
            self.src.close()
        except Exception:  # noqa: BLE001
            pass


def _make_shaper(p: dict):
    """ノイズシェーピングの量子化器(設定が有効で C ライブラリが使える時だけ)。使えなければ None(通常の TPDF ディザ)。"""
    name = p.get("nshape") or "off"
    vb = int(p.get("valid_bits", 32))
    if name == "off" or vb >= 32:
        return None
    try:
        from .nshape import NoiseShaper

        return NoiseShaper(name, int(p.get("out_channels", 2)), vb, bool(p.get("dither", True)), int(p.get("seed", 1)))
    except Exception:  # noqa: BLE001
        return None


class _NoDec:
    def close(self):
        pass


class DriftResampler:
    """ごくわずかな比率(±1000ppm 以内)で連続的に補正するための小さなリサンプラ(窓付き sinc の分数遅延)。
    キャプチャ側と DAC のクロック差を、1フレームの間引き・複製ではなく滑らかに吸収するために使う。
    入力は float64(各チャンネル -1..1)、ratio = 1 出力フレームあたりに消費する入力フレーム数。"""

    TAPS = 64
    PHASES = 512
    _table = None

    def __init__(self, channels: int):
        self.ch = channels
        T = self.TAPS
        self.hist = np.zeros((T, channels))
        self.pos = float(T // 2)        # x(= hist + 新しい入力)上の次の出力位置
        if DriftResampler._table is None:
            ph = np.arange(self.PHASES + 1) / self.PHASES
            k = np.arange(T) - T // 2 + 1                       # 入力フレームの相対位置
            t = k[None, :] - ph[:, None]                        # 出力位置からの距離
            w = np.kaiser(T + 1, 9.0)                           # 窓(中心が距離0、半幅 = タップ数/2)
            win = np.interp(t, np.arange(-(T // 2), T // 2 + 1), w, left=0.0, right=0.0)
            h = np.sinc(t) * win
            h /= h.sum(axis=1, keepdims=True)
            DriftResampler._table = h
        self.h = DriftResampler._table

    def process(self, x: np.ndarray, ratio: float) -> np.ndarray:
        T = self.TAPS
        buf = np.concatenate([self.hist, x], axis=0)
        n = buf.shape[0]
        lim = n - T // 2
        m = int(math.ceil((lim - self.pos) / ratio)) if lim > self.pos else 0
        if m <= 0:
            self.hist = buf[-T:] if n >= T else np.concatenate([self.hist, x], axis=0)[-T:]
            self.pos -= (n - T) if n >= T else 0
            return np.zeros((0, self.ch))
        p = self.pos + ratio * np.arange(m)
        i0 = np.floor(p).astype(np.int64)
        fp = (p - i0) * self.PHASES
        ph = np.floor(fp).astype(np.int64)
        fr = (fp - ph)[:, None]                                 # 位相テーブルの線形補間(位相量子化の歪みを避ける)
        idx = i0[:, None] - T // 2 + 1 + np.arange(T)[None, :]
        np.clip(idx, 0, n - 1, out=idx)
        taps = buf[idx]                                         # (m, T, ch)
        hk = self.h[ph] * (1.0 - fr) + self.h[np.minimum(ph + 1, self.PHASES)] * fr
        y = np.einsum("mtc,mt->mc", taps, hk)
        self.pos = self.pos + ratio * m - (n - T)
        self.hist = buf[-T:]
        return y


class LiveJob:
    """他アプリの音声(parec が吐く raw s32le)を読み、デバイス形式へ変換してリングに書く。

    RAM再生・リサンプルは行わない。キャプチャ側(null シンクのクロック)と DAC のクロックは独立なので、
    リングの溜まり具合が目標からずれたら 1 フレームずつ捨てる/重ねて吸収する(ppm オーダーのずれ前提)。
    """

    def __init__(self, p: dict):
        import fcntl
        import subprocess

        self.p = p
        self.prio = 0
        self.id = p["job_id"]
        self.src = SharedSource.attach(p["shm_name"], p["frame_bytes"])
        self.fb = p["frame_bytes"]
        self.in_ch = int(p.get("in_channels", 2))
        self.out_ch = p["out_channels"]
        self.fmt = p["fmt"]
        self.valid_bits = p["valid_bits"]
        self.dither = bool(p.get("dither", True))
        self.rng = np.random.default_rng(p.get("seed", 1))
        self.shaper = _make_shaper(p)
        self.target = int(p["target_frames"])
        self.dec = _NoDec()
        self.closed = False
        self.reported = False
        self.error: str | None = None
        self.eof = False
        self.left = b""
        self.last_adj = 0.0
        self._adv = 0               # 再生開始前に自分で進めた読み出し位置(これ以下なら出力はまだ読んでいない)
        self.dropped = 0
        self.duped = 0
        self.sync = p.get("sync", "slip")                 # "ratio": 連続補正 / "slip": 1フレームの間引き・複製
        self.rs = DriftResampler(self.out_ch) if self.sync == "ratio" else None
        self.rate = int(p.get("rate", 48000))
        self.eps = 0.0                                    # 現在の補正量(比率 - 1)
        self.eps_i = 0.0
        self.fill_ema = None
        self.t_prev = time.monotonic()
        self.t_stat = time.monotonic()
        self.fmin = None
        self.fmax = 0
        self.stat_q: list = []                            # 親へ送る統計(producer_main が取り出す)
        self.proc = subprocess.Popen(p["cmd"], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, bufsize=0)
        fd = self.proc.stdout.fileno()
        fcntl.fcntl(fd, fcntl.F_SETFL, fcntl.fcntl(fd, fcntl.F_GETFL) | os.O_NONBLOCK)
        self.fd = fd

    def step(self) -> bool:
        if self.closed or self.src.done:
            return False
        import select

        r, _, _ = select.select([self.fd], [], [], 0.005)
        if not r:
            if self.proc.poll() is not None:
                self.error = "キャプチャが終了しました(アプリが止まった/parec が終了)"
                self.src.mark_done(error=True)
                return True
            return False
        try:
            data = os.read(self.fd, 65536)
        except BlockingIOError:
            return False
        if not data:
            self.error = "キャプチャが終了しました"
            self.src.mark_done(error=True)
            return True
        data = self.left + data
        in_fb = 4 * self.in_ch
        n = len(data) // in_fb
        self.left = data[n * in_fb:]
        if n == 0:
            return True
        blk = np.frombuffer(data[:n * in_fb], dtype="<i4").reshape(n, self.in_ch)
        blk = map_channels(blk, self.out_ch)
        fill = self.src.available()
        now = time.monotonic()
        if self.fmin is None or fill < self.fmin:
            self.fmin = fill
        self.fmax = max(self.fmax, fill)
        if self.rs is not None:
            # 連続補正: 溜まり(平滑化)と目標の差から、PI 制御で比率を決める
            dt = min(0.5, max(0.0, now - self.t_prev))
            self.t_prev = now
            self.fill_ema = fill if self.fill_ema is None else self.fill_ema + (fill - self.fill_ema) * min(1.0, dt / 1.0)
            e = self.fill_ema - self.target
            self.eps_i = max(-1e-3, min(1e-3, self.eps_i + e / (self.rate * 400.0) * dt))
            self.eps = max(-1e-3, min(1e-3, e / (self.rate * 20.0) + self.eps_i))
            xf = blk.astype(np.float64) * (1.0 / 2147483648.0)
            yf = self.rs.process(xf, 1.0 + self.eps)
            if yf.shape[0] == 0:
                return True
            blk = np.clip(np.rint(yf * 2147483648.0), -2147483648, 2147483647).astype(np.int32)
        y = convert.requantize_left_i32(np.ascontiguousarray(blk), self.valid_bits, self.dither, self.rng, self.shaper)
        if now - self.t_stat >= 1.0:                       # 1秒ごとに統計を親へ
            self.stat_q.append({"dropped": self.dropped, "duped": self.duped, "ppm": round(self.eps * 1e6, 1),
                                "fill_min": self.fmin if self.fmin is not None else fill, "fill_max": self.fmax,
                                "target": self.target, "sync": self.sync})
            self.t_stat, self.fmin, self.fmax = now, None, 0
        # 大きなずれ・再生開始前の持ち越しの整理(連続補正でも最後の手段として残す)
        rp = self.src.read_pos
        if rp <= self._adv and fill + y.shape[0] > self.target and fill > 0:
            # 再生開始前(デバイスを開く/無音の準備中)に溜まった古い分を先頭から捨てて、遅延を持ち越さない
            # (新しい音を捨てると、再生が始まった後に音が飛ぶ)
            drop = min(fill, fill + y.shape[0] - self.target)
            self.src.read_pos = rp + drop
            self._adv = rp + drop
            fill -= drop
        band = max(64, self.target // 4)
        if self.rs is not None:
            band = max(band * 8, int(self.rate * 0.05))                  # 連続補正中は大きく外れた時だけ介入
        interval = 0.005 if fill > self.target + band * 4 else 0.05      # 大きくずれている時は速く寄せる
        if now - self.last_adj > interval:
            if fill > self.src.capacity * 0.8:                 # 溢れそう: 目標まで一気に詰める
                cut = min(y.shape[0], fill - self.target)
                y = y[cut:] if cut > 0 else y
                self.dropped += max(0, cut)
                self.last_adj = now
            elif fill > self.target + band and y.shape[0] > 1:
                y = y[1:]
                self.dropped += 1
                self.last_adj = now
            elif fill < max(self.target - band, 0) and y.shape[0] > 0:
                y = np.concatenate([y, y[-1:]], axis=0)
                self.duped += 1
                self.last_adj = now
        out = convert.pack(y, self.fmt)
        w = self.src.write(out)
        if w * self.fb < out.shape[0]:
            self.dropped += (out.shape[0] // self.fb) - w
        return True

    def close(self):
        if self.closed:
            return
        self.closed = True
        try:
            self.proc.terminate()
            self.proc.wait(timeout=1.5)
        except Exception:  # noqa: BLE001
            try:
                self.proc.kill()
            except Exception:  # noqa: BLE001
                pass
        try:
            self.src.close()
        except Exception:  # noqa: BLE001
            pass


def producer_main(conn, cfg: dict) -> None:
    try:
        from . import sysinfo

        if cfg.get("cpus"):
            sysinfo.confine_process(set(cfg["cpus"]))
        nice = cfg.get("nice", 5)
        if nice:
            try:
                os.nice(nice)
            except OSError:
                pass
    except Exception:  # noqa: BLE001
        pass
    jobs: "OrderedDict[int, Job]" = OrderedDict()
    running = True
    idle_poll = 0.01
    while running:
        try:
            while conn.poll(0):
                msg = conn.recv()
                op = msg[0]
                if op == "add":
                    p = msg[1]
                    try:
                        jobs[p["job_id"]] = LiveJob(p) if p.get("live") else Job(p)
                    except Exception as e:  # noqa: BLE001
                        conn.send(("error", p["job_id"], f"{type(e).__name__}: {e}"))
                elif op == "opt":                           # 静音モード等: 暇な時の待機間隔
                    idle_poll = max(0.005, min(0.2, float(msg[1].get("idle_poll", idle_poll))))
                elif op == "live_ctl":                      # 動作中の取り込みの設定変更(遅延の自動調整など)
                    j = jobs.get(msg[1])
                    if isinstance(j, LiveJob):
                        if "target" in msg[2]:
                            j.target = int(msg[2]["target"])
                elif op == "cancel":
                    j = jobs.pop(msg[1], None)
                    if j is not None:
                        j.close()
                elif op == "affinity":
                    try:
                        from . import sysinfo

                        sysinfo.confine_process(set(msg[1]))
                    except Exception:  # noqa: BLE001
                        pass
                elif op == "quit":
                    running = False
                    break
            busy = False
            for j in sorted(jobs.values(), key=lambda j: j.prio):
                if j.step():
                    busy = True
                if isinstance(j, LiveJob) and j.stat_q:
                    conn.send(("live_stats", j.id, j.stat_q[-1]))
                    j.stat_q.clear()
                if j.src.done and not j.reported:
                    j.reported = True
                    if j.error:
                        conn.send(("error", j.id, j.error))
                    else:
                        conn.send(("eof", j.id))
                    j.dec.close()
                if busy:
                    break
            if not busy:
                conn.poll(0.01 if any(isinstance(j, LiveJob) for j in jobs.values()) else idle_poll)
        except (EOFError, BrokenPipeError, ConnectionResetError):
            break
        except Exception:  # noqa: BLE001
            traceback.print_exc()
            time.sleep(0.05)
    for j in jobs.values():
        j.close()
