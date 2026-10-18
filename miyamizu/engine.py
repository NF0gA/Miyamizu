"""再生エンジン(GUI とは別プロセス)。

  制御スレッド(main) : ローカルソケットで GUI と JSON 行をやり取り。計画・ソース生成・状態通知。
  出力スレッド       : 共有メモリから読んで write するだけ。専用コア固定・RT優先度の対象。
  準備プロセス       : デコード/リサンプル/形式変換(producer.py)。出力スレッドと別コア。

単体でも動く:  python -m miyamizu.engine --socket /path/to.sock
"""
from __future__ import annotations

import argparse
import json
import math
import multiprocessing as mp
import os
import queue
import selectors
import signal
import socket
import sys
import threading
import time
import traceback
from collections import deque
from dataclasses import dataclass, field, replace

import numpy as np

from shutil import which as shutil_which

from . import __version__, convert, pwutil, soxr_ctypes, sysinfo

if sys.platform == "win32":
    from . import wasapi as alsa          # 同じ関数名(list_hw_devices/probe_caps/probe_format)で WASAPI を提供する
else:
    from . import alsa
from .backends import AlsaBackend, Backend, SessionParams, make_backend
from .common import STD_RATES, default_socket_path, rate_label, use_tcp
from .decode import DecodeError, probe
from .producer import producer_main
from .settings import Settings
from .shm_ring import SharedSource

MAX_CH = 8


class EngineError(Exception):
    pass


# ====================================================================== レート決定
def _family_rates(rate: int) -> list[int]:
    base = 44100 if rate % 11025 == 0 else 48000
    return [base * (2 ** k) for k in range(4)]


def _best_family_leq(rate: int, cap: int) -> int:
    fam = [r for r in _family_rates(rate) if r <= cap]
    return max(fam) if fam else min(STD_RATES)


def decide_rate(src_rate: int, s: Settings) -> tuple[int, list[str]]:
    """設定とソースのレートから出力レートを決める。(レート, 強制変換の理由リスト)"""
    cap = s.rate_cap
    forced: list[str] = []
    if not s.resample_enabled:
        if src_rate <= cap:
            return src_rate, forced
        r = _best_family_leq(src_rate, cap)
        forced.append(f"ソース{rate_label(src_rate)}が出力上限{rate_label(cap)}を超えるため強制ダウンサンプル")
        return r, forced
    mode = s.resample_mode
    if mode == "fixed":
        r = min(s.fixed_rate, cap)
        return r, forced
    if mode in ("x2", "x4"):
        r = src_rate * (2 if mode == "x2" else 4)
        if r > cap:
            r = _best_family_leq(src_rate, cap)
        return r, forced
    # family_max
    return _best_family_leq(src_rate, cap), forced


def fallback_rate(want: int, supported: list[int], cap: int) -> int | None:
    """デバイスが want を受けない時の代替。同系列の上位 → 他系列の上位 → 最大。"""
    ok = [r for r in supported if r <= cap]
    if not ok:
        return None
    fam = _family_rates(want)
    same = sorted(r for r in ok if r in fam and r >= want)
    if same:
        return same[0]
    higher = sorted(r for r in ok if r >= want)
    if higher:
        return higher[0]
    return max(ok)


PHASE_JA = {"linear": "線形位相", "intermediate": "中間位相", "minimum": "最小位相"}


R8_PRESET_JA = {"standard": "標準", "minphase": "最小位相", "fast": "高速", "custom": "詳細"}


def resample_label(s: Settings) -> str:
    from . import r8brain, soxr_ctypes

    if s.resample_engine == "r8brain" and r8brain.available():
        tb, att, ph = r8brain.params(s.r8_preset, s.r8_trans_band, s.r8_atten_db, s.r8_phase)
        hr = f" ヘッドルーム{s.r8_headroom_db:g}dB" if s.r8_headroom_db < 0 else ""
        return f"r8brain {R8_PRESET_JA.get(s.r8_preset, s.r8_preset)}(遷移帯域{tb:g}% 減衰{att:g}dB {PHASE_JA.get(ph, ph)}){hr}"

    q = {"32": "32bit精度"}.get(s.resample_quality, s.resample_quality)
    if not soxr_ctypes.available():
        return f"soxr {q if s.resample_quality in ('QQ', 'LQ', 'MQ', 'HQ', 'VHQ') else 'VHQ'}(libsoxr 無し: 線形位相・標準帯域)"
    return f"soxr {q} {PHASE_JA.get(s.resample_phase, s.resample_phase)}" + (" 広帯域" if s.resample_band == "wide" else "")


# ====================================================================== データ型
@dataclass
class Plan:
    params: SessionParams
    info: dict
    src_rate: int
    out_rate: int
    out_ch: int
    resample: bool
    forced: list[str]


class Source:
    def __init__(self, sid: int, track: dict, shm: SharedSource, plan: Plan, start_offset: int,
                 dur_s: float, ram: bool, signal: dict, ready_frames: int):
        self.id = sid
        self.track = track
        self.shm = shm
        self.plan = plan
        self.out_rate = plan.out_rate
        self.start_offset = start_offset    # トラック先頭からの出力フレーム数
        self.dur_s = dur_s
        self.ram = ram
        self.signal = signal
        self.started = False
        self.ready_frames = ready_frames

    def ready(self) -> bool:
        sh = self.shm
        return sh.done or sh.available() >= self.ready_frames


# ====================================================================== 出力スレッド
HIST_EDGES_US = [10, 50, 100, 250, 500, 1000, 2000, 5000]   # 周期ずれの階級(µs)


class OutputThread(threading.Thread):
    def __init__(self, engine: "Engine"):
        super().__init__(name="miyamizu-output", daemon=True)
        self.eng = engine
        self.cmds: "queue.SimpleQueue" = queue.SimpleQueue()
        self.events = engine.events
        self.backend: Backend | None = None
        self.params: SessionParams | None = None
        self.cur: Source | None = None
        self.next: Source | None = None
        self.paused = False
        self.quit = False
        self.timeline: deque = deque()
        self.stream_pos = 0
        self.delay_frames = 0
        self.period_frames = 0
        self.buf: np.ndarray | None = None
        self._running = False
        self._last_id = 0
        self._last_base = 0
        self.idle_frames = 0
        self._last_t0 = 0
        self._skip_stats = 0
        self._period_n = 0
        self.rt_status = "未設定"
        self.affinity_status = "未設定"
        self.idle_close_s = 30.0
        self.lead_in_frames = 0        # デバイスを開き直した直後に流す無音の残り(DDC/DAC のロック待ち)
        self.reset_stats()

    # ---- 統計 ----
    def reset_stats(self):
        self.hist = [0] * (len(HIST_EDGES_US) + 1)
        self.periods = 0
        self.underruns = 0
        self.dev_sum_ns = 0
        self.dev_max_ns = 0

    def snapshot(self) -> dict:
        be = self.backend
        v, nv = sysinfo.thread_ctxt_switches(self.native_id) if self.native_id else (0, 0)
        cur = self.cur
        fill = None
        if cur is not None:
            sh = cur.shm
            fill = [sh.available(), sh.capacity, 1 if sh.ring else 0]
        return {
            "periods": self.periods,
            "hist": list(self.hist),
            "hist_edges_us": HIST_EDGES_US,
            "dev_mean_us": (self.dev_sum_ns / self.periods / 1000.0) if self.periods else 0.0,
            "dev_max_us": self.dev_max_ns / 1000.0,
            "underruns": self.underruns,
            "xruns": be.xruns() if be is not None else 0,
            "ctxt_voluntary": v,
            "ctxt_involuntary": nv,
            "rt": self.rt_status,
            "affinity": self.affinity_status,
            "period_frames": self.period_frames,
            "buffer_frames": be.buffer_frames if be is not None else 0,
            "delay_frames": self.delay_frames,
            "fill": fill,
        }

    # ---- RT/アフィニティ ----
    def _rt_setup(self, ded: set[int], prio: int, idle_close_s: float):
        self.idle_close_s = idle_close_s
        if ded:
            ok = sysinfo.set_thread_affinity(ded)
            self.affinity_status = f"専用CPU {sorted(ded)}" if ok else "固定失敗"
        else:
            sysinfo.set_thread_affinity(set(sysinfo.online_cpus()))     # 以前の固定を解除
            self.affinity_status = "固定なし"
        if prio > 0:
            ok, msg = sysinfo.try_realtime(prio)
            self.rt_status = msg if ok else f"RT無効 ({msg}) 対処: {sysinfo.rt_hint()}"
            print(f"[rt] {self.rt_status}", file=sys.stderr, flush=True)
        else:
            self.rt_status = "RT無効 (設定)"

    # ---- タイムライン ----
    def _mark(self, boundary: int, sid: int, base: int, running: bool):
        self.timeline.append((boundary, sid, base, running))
        self._running = running

    def _retire(self, src: Source | None):
        if src is not None:
            self.events.put(("retire", src.id))

    # ---- コマンド ----
    def _drain_cmds(self, block: float = 0.0):
        while True:
            try:
                cmd = self.cmds.get(timeout=block) if block else self.cmds.get_nowait()
            except queue.Empty:
                return
            block = 0.0
            op = cmd[0]
            if op == "open":
                self._do_open(cmd[1], cmd[2])
            elif op == "close":
                self._close_backend(True)
                cmd[1].result = None
                cmd[1].set()
            elif op == "play":
                src, paused = cmd[1], cmd[2]
                self._retire(self.cur)
                self._retire(self.next)
                self.cur, self.next = src, None
                src.started = False
                self.paused = paused
                self.idle_frames = 0
                self._running = False
            elif op == "set_next":
                self._retire(self.next)
                self.next = cmd[1]
            elif op == "pause":
                self.paused = bool(cmd[1])
            elif op == "stop":
                self._retire(self.cur)
                self._retire(self.next)
                self.cur = self.next = None
                self.paused = False
            elif op == "seek_local":
                if self.cur is not None:
                    self.cur.shm.read_pos = max(0, int(cmd[1]))
                    self._running = False
            elif op == "rt":
                self._rt_setup(cmd[1], cmd[2], cmd[3])
            elif op == "quit":
                self.quit = True
                return

    def _do_open(self, params: SessionParams, evt):
        try:
            if self.backend is not None and self.params == params:
                evt.result = None
                evt.set()
                return
            self._close_backend(True)
            be = self.eng.backend_factory()
            be.open(params)
            self.backend, self.params = be, params
            self.period_frames = be.period_frames
            self.buf = np.zeros(be.period_frames * params.frame_bytes, dtype=np.uint8)
            self.stream_pos = 0
            self.timeline.clear()
            self._running = False
            self.delay_frames = 0
            self._skip_stats = be.buffer_frames // max(1, be.period_frames) + 3
            self._last_t0 = 0
            self._period_n = 0
            self.idle_frames = 0
            lead_ms = float(getattr(self.eng.settings, "lead_in_ms", 0.0) or 0.0)
            self.lead_in_frames = int(lead_ms / 1000.0 * params.rate) if (lead_ms > 0 and params.kind in ("alsa_hw", "custom")) else 0
            self.reset_stats()
            evt.result = None
        except Exception as e:  # noqa: BLE001
            self.backend = None
            self.params = None
            evt.result = f"{type(e).__name__}: {e}"
        evt.set()

    def _close_backend(self, drain: bool):
        be = self.backend
        self.backend = None
        self.params = None
        if be is not None:
            try:
                be.close(drain)
            except Exception:  # noqa: BLE001
                pass

    # ---- 1 ピリオド ----
    def _fill(self, buf: np.ndarray, P: int) -> int:
        pos = 0
        while pos < P:
            src = self.cur
            if src is None or self.paused:
                break
            sh = src.shm
            if not src.started:
                if src.ready():
                    src.started = True
                else:
                    if self._running:     # 再生の途中で次ソースが間に合わなかった
                        self.underruns += 1
                    break
            if not self._running:
                self._mark(self.stream_pos + pos, src.id, sh.read_pos, True)
            n = sh.read_into(buf, pos, P - pos)
            pos += n
            self._last_id, self._last_base = src.id, sh.read_pos
            if pos < P:
                if sh.finished():
                    end_b = self.stream_pos + pos
                    nxt = self.next
                    self.events.put(("source_end", src.id))
                    self.next = None
                    if nxt is not None:
                        self.cur = nxt
                        nxt.started = False
                        self.events.put(("advanced", src.id, nxt.id))
                        self._mark(end_b, nxt.id, 0, True)
                        self._last_id, self._last_base = nxt.id, 0
                        continue
                    self.cur = None
                    self.events.put(("queue_empty", src.id))
                    break
                self.underruns += 1
                break
        if pos < P and self._running:
            self._mark(self.stream_pos + pos, self._last_id, self._last_base, False)
        return pos

    def _period(self):
        P = self.period_frames
        fb = self.params.frame_bytes
        buf = self.buf
        if self.lead_in_frames > 0:       # 開き直し直後は無音だけを流して、受け側のロックを待つ
            self.lead_in_frames -= P
            pos = 0
        else:
            pos = self._fill(buf, P)
        if pos < P:
            buf[pos * fb:P * fb] = 0
        t0 = time.perf_counter_ns()
        try:
            self.backend.write(buf, P)
        except Exception as e:  # noqa: BLE001
            self.events.put(("device_error", f"{type(e).__name__}: {e}"))
            self._close_backend(False)
            return
        self.stream_pos += P
        self._period_n += 1
        if self._skip_stats > 0:
            self._skip_stats -= 1
        else:
            if self._last_t0:
                nominal = P * 1_000_000_000 // self.params.rate
                dev = abs((t0 - self._last_t0) - nominal)
                self.periods += 1
                self.dev_sum_ns += dev
                if dev > self.dev_max_ns:
                    self.dev_max_ns = dev
                us = dev / 1000.0
                i = 0
                while i < len(HIST_EDGES_US) and us >= HIST_EDGES_US[i]:
                    i += 1
                self.hist[i] += 1
        self._last_t0 = t0
        if (self._period_n & 3) == 0:
            self.delay_frames = self.backend.delay()
        # アイドル管理
        if self.cur is None and not self.paused:
            self.idle_frames += P
            if self.idle_close_s > 0 and self.idle_frames >= self.idle_close_s * self.params.rate:
                self._close_backend(True)
                self.events.put(("device_closed",))
        else:
            self.idle_frames = 0

    def run(self):
        try:
            while not self.quit:
                if self.backend is None:
                    self._drain_cmds(block=0.05)
                    continue
                self._drain_cmds()
                if self.quit:
                    break
                if self.backend is not None:
                    self._period()
        except Exception:  # noqa: BLE001
            traceback.print_exc()
            self.events.put(("device_error", "出力スレッドが異常終了しました"))
        finally:
            self._close_backend(False)


# ====================================================================== エンジン本体
class Client:
    def __init__(self, sock: socket.socket, authed: bool = True):
        self.sock = sock
        self.buf = b""
        self.authed = authed            # TCP 接続は最初の1行で認証トークンを送るまで未認証


class Engine:
    def __init__(self, sock_path: str | None = None, test_backend: str | None = None,
                 test_format: str = "S32_LE", enable_rt: bool = True):
        self.sock_path = sock_path or default_socket_path()
        self.settings = Settings()
        self.events: "queue.SimpleQueue" = queue.SimpleQueue()
        self.test_mode = bool(test_backend)
        self.test_format = test_format
        self.enable_rt = enable_rt
        self._test_backend_obj: Backend | None = make_backend(test_backend) if test_backend else None
        self.out = OutputThread(self)
        self.sel = selectors.DefaultSelector()
        self.clients: list[Client] = []
        self.quit = False
        self.sources: dict[int, Source] = {}
        self.meta: dict[int, dict] = {}
        self._sid = 0
        self.cur_src: Source | None = None
        self.next_src: Source | None = None
        self.cur_track: dict | None = None
        self.next_track: dict | None = None
        self.audible_id = 0
        self.volume = 1.0
        self.state = "stopped"
        self.paused = False
        self._suspended: list[str] = []
        self._forced_rate = False
        self._pw_device: str | None = None
        self._ded: set[int] = set()
        self._reserved: set[int] = set()
        self._susp_cards: set[int] = set()
        self._ahead: dict[tuple, Source] = {}          # 実験: 次曲より先に変換済みの曲 (キー: path,start,end)
        self.ahead_tracks: list[dict] = []
        self._profiles_off: dict[int, tuple[str, str]] = {}      # card_index → (カード名, 元のプロファイル)
        self._touched_apps: set = set()                      # 取り込みで出力先を動かしたアプリ(戻す対象)
        self._cap: dict | None = None                        # アプリ音声取り込みの状態(null シンク等)
        self._claimed: set[int] = set()                          # 排他で専有中のカード(アイドルで閉じてもプロファイルを戻さない)
        self._asg_mode: str | None = None
        self._asg_main: int | None = None
        self.assignment: dict = {}
        self._others: set[int] = set(sysinfo.online_cpus())
        self.last_status: dict = {}
        self.prod_conn = None
        self.prod_proc = None
        self._restore_pending_profiles()
        if not self.test_mode:
            pwutil.capture_cleanup_stale()
            from .common import purge_old_downloads
            purge_old_downloads()
        self._start_producer()

    # ---- 前回の異常終了で off のまま残ったカードプロファイルを戻す ----
    @staticmethod
    def _pending_path():
        from .common import data_dir

        return data_dir() / "pending_card_profiles.json"

    def _save_pending(self):
        import json as _json

        p = self._pending_path()
        try:
            if self._profiles_off:
                p.write_text(_json.dumps({str(k): list(v) for k, v in self._profiles_off.items()}))
            elif p.exists():
                p.unlink()
        except OSError:
            pass

    def _restore_pending_profiles(self):
        import json as _json

        p = self._pending_path()
        try:
            data = _json.loads(p.read_text())
        except (OSError, ValueError):
            return
        for _idx, (name, prof) in data.items():
            if prof and prof != "off":
                pwutil.set_card_profile(name, prof)
        try:
            p.unlink()
        except OSError:
            pass

    # ---- 準備プロセス ----
    def _start_producer(self):
        ctx = mp.get_context("spawn")
        parent, child = ctx.Pipe(duplex=True)
        cfg = {"cpus": sorted(self._others), "nice": 5}
        self.prod_proc = ctx.Process(target=producer_main, args=(child, cfg), daemon=True, name="miyamizu-producer")
        self.prod_proc.start()
        child.close()
        self.prod_conn = parent
        if getattr(self, "settings", None) is not None:
            self._apply_quiet()

    def backend_factory(self) -> Backend:
        if self._test_backend_obj is not None:
            return self._test_backend_obj
        if sys.platform == "win32":
            from .backends import WasapiBackend
            return WasapiBackend()
        return AlsaBackend()

    # ---- ブロードキャスト ----
    def emit(self, ev: str, **kw):
        data = (json.dumps({"ev": ev, **kw}, ensure_ascii=False) + "\n").encode()
        for c in list(self.clients):
            try:
                c.sock.settimeout(0.5)
                c.sock.sendall(data)
            except OSError:
                self._drop_client(c)

    def _drop_client(self, c: Client):
        try:
            self.sel.unregister(c.sock)
        except Exception:  # noqa: BLE001
            pass
        try:
            c.sock.close()
        except OSError:
            pass
        if c in self.clients:
            self.clients.remove(c)

    def reply(self, c: Client, ev: str, **kw):
        try:
            c.sock.settimeout(0.5)
            c.sock.sendall((json.dumps({"ev": ev, **kw}, ensure_ascii=False) + "\n").encode())
        except OSError:
            self._drop_client(c)

    # ---- CPU 計画 ----
    def apply_cpu_plan(self):
        s = self.settings
        ded, others = sysinfo.plan_cpus(s.dedicated_cpu, s.reserve_sibling)
        self._ded, self._others = ded, others
        self._reserved = sysinfo.reserved_siblings(s.dedicated_cpu, s.reserve_sibling) if ded else set()
        if s.confine_gui:
            sysinfo.confine_process(others)           # 制御スレッドと全スレッドを一旦外す(専用なしなら全CPUへ戻す)
            try:
                self.prod_conn.send(("affinity", sorted(others)))
            except (OSError, BrokenPipeError):
                pass
        prio = s.rt_priority if self.enable_rt else 0
        self.out.cmds.put(("rt", set(ded), prio, s.idle_close_s))

    # ---- デバイス ----
    def _kind(self) -> str:
        return "test" if self.test_mode else self.settings.backend

    def _device_name(self) -> str:
        s = self.settings
        if self.test_mode:
            return "test"
        if s.backend == "alsa_hw":
            if s.alsa_device:
                return s.alsa_device
            devs = alsa.list_hw_devices()
            if not devs:
                raise EngineError("ALSAのハードウェアデバイスが見つかりません")
            return devs[0].name
        if s.backend == "custom":
            return s.custom_device or "default"
        if sys.platform == "win32":
            return "default"            # WASAPI 共有: 既定の出力デバイス
        if self._pw_device is None:
            try:
                self._pw_device = "pipewire" if alsa.probe_caps("pipewire").ok else "default"
            except Exception:  # noqa: BLE001
                self._pw_device = "default"
        return self._pw_device

    def _card_index(self, device: str) -> int | None:
        for d in alsa.list_hw_devices():
            if d.name == device:
                return d.card_index
        import re

        m = re.match(r"hw:(\d+)", device)
        return int(m.group(1)) if m else None

    def _probe_with_retry(self, device: str) -> alsa.Caps:
        s = self.settings
        idx = self._card_index(device)
        if s.release_pipewire and idx is not None:
            self._suspended += pwutil.suspend_sinks_for_card(idx)
            self._susp_cards.add(idx)
        t0 = time.monotonic()
        deadline = t0 + s.busy_retry_s
        while True:
            caps = alsa.probe_caps(device)
            if caps.ok:
                return caps
            if caps.busy and time.monotonic() < deadline:
                if idx is not None and time.monotonic() - t0 > 1.2 and self._force_release(idx):
                    continue
                if idx is not None and s.release_pipewire and time.monotonic() - t0 < 3.0:
                    self._suspended += pwutil.suspend_sinks_for_card(idx)       # 起こされていたら再度 suspend
                time.sleep(0.4)
                continue
            hint = self._busy_hint(idx) or ("(他のアプリ/PipeWire が使用中の可能性)" if caps.busy else "")
            raise EngineError(f"デバイス {device} を開けません: {caps.error} {hint}")

    def _close_device(self):
        if self.out.backend is None:
            return
        evt = threading.Event()
        evt.result = None
        self.out.cmds.put(("close", evt))
        evt.wait(5)

    def _cleanup_device_state(self, keep_claim: bool = False):
        if self._suspended or self._susp_cards:
            pwutil.resume_sinks(self._suspended)
            for idx in self._susp_cards:        # 名前が変わった/消えたシンクも含め、カード単位で復帰させる
                pwutil.resume_card(idx)
            self._suspended = []
            self._susp_cards = set()
        if self._profiles_off:
            for idx, (name, prof) in list(self._profiles_off.items()):
                if keep_claim and idx in self._claimed:
                    continue                    # 専有中: 排他→共有へ戻すまでプロファイル off を保つ
                if prof and prof != "off":
                    pwutil.set_card_profile(name, prof)
                # 復元後に suspend が残っていると OS のミキサーに出ないので、念のため解除
                pwutil.resume_card(idx)
                del self._profiles_off[idx]
            if not keep_claim:
                self._claimed = set()
            self._save_pending()
        if self._forced_rate:
            pwutil.set_force_rate(0)
            self._forced_rate = False

    def _force_release(self, idx: int) -> bool:
        """suspend でも解放できない時の最終手段: カードのプロファイルを off にして PipeWire に手を離させる。"""
        if idx in self._profiles_off or not self.settings.release_profile_off or self.test_mode:
            return False
        card = pwutil.find_card(idx)
        if not card or not card.get("name"):
            return False
        prof = card.get("active_profile") or ""
        if prof == "off":
            return False
        self._profiles_off[idx] = (card["name"], prof)
        self._save_pending()                  # 異常終了しても次回起動で戻せるように先に記録
        if not pwutil.set_card_profile(card["name"], "off"):
            self._profiles_off.pop(idx, None)
            self._save_pending()
            return False
        time.sleep(0.4)
        return True

    # ---- メイン機器の割り当て(共有 ⇄ 排他) ----
    def _main_card(self) -> int | None:
        try:
            dev = self.settings.alsa_device
            if not dev:
                devs = alsa.list_hw_devices()
                dev = devs[0].name if devs else ""
            return self._card_index(dev) if dev else None
        except Exception:  # noqa: BLE001
            return None

    def _claim_main(self) -> dict:
        """排他: メイン機器を OS(PipeWire)側から外す。プロファイルを off にし、共有へ戻すまで保つ。"""
        idx = self._main_card()
        if idx is None:
            return dict(ok=False, msg="メイン機器が見つかりません")
        if idx in self._claimed:
            return dict(ok=True, card=idx, msg="専有済み")
        if self.settings.release_pipewire:
            self._suspended += pwutil.suspend_sinks_for_card(idx)
            self._susp_cards.add(idx)
        if self._force_release(idx) or idx in self._profiles_off:
            self._claimed.add(idx)
            return dict(ok=True, card=idx, msg="排他に割り当て(OSのミキサーから外しました)")
        return dict(ok=False, card=idx, msg="OS側から外せませんでした(使用中の場合は再生時に再試行します)")

    def _assign_shared(self) -> dict:
        """共有: メイン機器を PipeWire に戻し、既定の出力にする。"""
        idx = self._main_card()
        if idx is None:
            return dict(ok=False, msg="メイン機器が見つかりません")
        sink = pwutil.wait_sink_for_card(idx)
        if not sink:
            return dict(ok=False, card=idx, msg="共有側にメイン機器のシンクが現れません")
        if not pwutil.set_default_sink(sink):
            return dict(ok=False, card=idx, sink=sink, msg="既定の出力を変更できませんでした")
        self._restore_apps(sink)
        return dict(ok=True, card=idx, sink=sink, msg="共有のメイン機器に割り当て")

    def apply_assignment(self):
        s = self.settings
        if self.test_mode:
            return
        mode = {"alsa_hw": "exclusive", "pipewire": "shared"}.get(s.backend, "custom")
        main = self._main_card()
        prev_mode, prev_main = self._asg_mode, self._asg_main
        self._asg_mode, self._asg_main = mode, main
        res = None
        if mode == "exclusive":
            if s.claim_on_switch:
                res = self._claim_main()
        elif mode == "shared" and s.default_on_shared and main is not None:
            if prev_mode == "exclusive" or (prev_mode is not None and prev_main != main):
                res = self._assign_shared()
        if res is not None:
            self.assignment = dict(mode=mode, **res)
            self.emit("assignment", **self.assignment)

    def _busy_hint(self, idx: int | None) -> str:
        if idx is None:
            return ""
        who = pwutil.pcm_owners(idx)
        return f"(使用中: {', '.join(who)})" if who else ""

    def _open_session(self, params: SessionParams):
        s = self.settings
        if self.out.backend is not None and self.out.params == params:
            return
        self._close_device()
        idx = self._card_index(params.device) if params.kind == "alsa_hw" else None
        if idx is not None and s.release_pipewire and not self._suspended:
            self._suspended += pwutil.suspend_sinks_for_card(idx)
            self._susp_cards.add(idx)
        if params.kind == "pipewire" and s.pw_force_rate:
            self._forced_rate = pwutil.set_force_rate(params.rate) or self._forced_rate
        deadline = time.monotonic() + (s.busy_retry_s if params.kind == "alsa_hw" else 1.0)
        while True:
            evt = threading.Event()
            evt.result = None
            self.out.cmds.put(("open", params, evt))
            evt.wait(15)
            if evt.result is None:
                return
            if "busy" in evt.result.lower() and time.monotonic() < deadline:
                if idx is not None and self._force_release(idx):
                    continue
                time.sleep(0.4)
                continue
            raise EngineError(f"出力を開始できません: {evt.result} {self._busy_hint(idx)}".rstrip())

    # ---- 計画 ----
    def _decide(self, info: dict, caps: alsa.Caps | None = None) -> tuple[int, int, bool, list[str]]:
        s = self.settings
        if self._cap is not None:           # アプリ音声の取り込み中はリサンプルしない
            from dataclasses import replace as _rep
            s = _rep(s, resample_enabled=False)
        src_rate, src_ch = info["rate"], info["channels"]
        rate, forced = decide_rate(src_rate, s)
        out_ch = min(src_ch, MAX_CH)
        if caps is not None and caps.ok:
            if caps.rates and rate not in caps.rates:
                fb = fallback_rate(rate, caps.rates, s.rate_cap)
                if fb is None:
                    raise EngineError("デバイスが対応するサンプルレートがありません")
                forced = forced + [f"デバイスが{rate_label(rate)}に非対応のため{rate_label(fb)}へ変換"]
                rate = fb
            if caps.ch_max and not (caps.ch_min <= out_ch <= caps.ch_max):
                out_ch = 2 if caps.ch_min <= 2 <= caps.ch_max else max(caps.ch_min, min(out_ch, caps.ch_max))
                forced = forced + [f"チャンネル数を{out_ch}に変換"]
        return rate, out_ch, rate != src_rate, forced

    def _prepare_session(self, info: dict) -> Plan:
        s = self.settings
        kind = self._kind()
        device = self._device_name()
        cur = self.out.params
        caps: alsa.Caps | None = None
        if kind == "alsa_hw":
            rate, out_ch, resample, forced = self._decide(info)
            if (self.out.backend is not None and cur is not None and cur.device == device
                    and cur.kind == kind and (cur.rate, cur.channels) == (rate, out_ch)):
                fmt = cur.fmt
            else:
                self._close_device()
                caps = self._probe_with_retry(device)
                self._caps_cache = caps
                rate, out_ch, resample, forced = self._decide(info, caps)
                fmt, err = alsa.probe_format(device, rate, out_ch)
                if fmt is None:
                    raise EngineError(f"{device} は {rate_label(rate)}/{out_ch}ch の出力形式に対応していません ({err})")
        else:
            rate, out_ch, resample, forced = self._decide(info)
            fmt = self.test_format if self.test_mode else "S32_LE"
        valid_bits = convert.default_valid_bits(fmt, s.s32_valid_bits)
        frame_bytes = convert.bytes_per_sample(fmt) * out_ch
        pm, pn = (s.capture_period_ms, s.capture_periods) if self._cap is not None else (s.period_ms, s.periods)
        params = SessionParams(kind, device, rate, out_ch, fmt, valid_bits, frame_bytes, pm, pn)
        self._open_session(params)
        return Plan(params, info, info["rate"], rate, out_ch, resample, forced)

    # ---- ソース生成 ----
    def _signal_path(self, plan: Plan, ram: bool, ring_note: str | None) -> dict:
        s = self.settings
        info, p = plan.info, plan.params
        src_label = f"{info['codec']} {rate_label(plan.src_rate)}"
        if info["is_int"]:
            src_label += f" {info['src_bits']}bit"
        src_label += f" {info['channels']}ch"
        steps: list[str] = []
        if plan.resample:
            steps.append(f"リサンプル {resample_label(s)}: {rate_label(plan.src_rate)} → {rate_label(plan.out_rate)}")
        if plan.out_ch != info["channels"]:
            steps.append(f"チャンネル変換 {info['channels']}→{plan.out_ch}ch")
        vol_active = (p.kind != "alsa_hw") and abs(self.volume - 1.0) > 1e-6
        if vol_active:
            steps.append(f"ソフト音量 {self.volume:.2f}")
        requant = (not info["is_int"]) or info["src_bits"] > p.valid_bits or plan.resample or vol_active
        if requant:
            steps.append(f"{p.valid_bits}bit へ量子化" + self._quant_note())
        bitperfect = (p.kind == "alsa_hw" and not steps)
        notes = list(plan.forced)
        if p.kind == "pipewire":
            notes.append("PipeWire共有: グラフ側でレート/形式が変換される場合があります"
                         + ("(clock.force-rate 適用)" if s.pw_force_rate else ""))
        if p.kind == "alsa_hw" and abs(self.volume - 1.0) > 1e-6:
            notes.append("ビットパーフェクトのため音量は0dB固定(本体ボリュームで調整)")
        if ring_note:
            notes.append(ring_note)
        return {
            "source": src_label,
            "steps": steps,
            "output": f"{p.fmt}({p.valid_bits}bit有効) {rate_label(p.rate)} {p.channels}ch",
            "device": p.device,
            "backend": p.kind,
            "bitperfect": bitperfect,
            "ram": ram,
            "notes": notes,
        }

    @staticmethod
    def _tkey(t: dict) -> tuple:
        return (t.get("path"), int(t.get("start_frame") or 0), t.get("end_frame"))

    def _probe_track(self, track: dict) -> dict:
        return probe(track["path"])

    def _make_source(self, track: dict, plan: Plan, start_s: float, prio: int = 0) -> Source:
        s = self.settings
        if prio == 0 and start_s == 0.0:      # 事前変換済みならそれを使う
            a = self._ahead.get(self._tkey(track))
            if (a is not None and a.plan.out_rate == plan.out_rate and a.plan.out_ch == plan.out_ch
                    and a.plan.params == plan.params and not a.shm.error):
                self._ahead.pop(self._tkey(track), None)
                a.track = track
                if a.id in self.meta:
                    self.meta[a.id]["track"] = track
                return a
        info, p = plan.info, plan.params
        src_rate = plan.src_rate
        start_f = int(track.get("start_frame", 0)) + int(start_s * src_rate)
        end_f = track.get("end_frame")
        end_eff = int(end_f) if end_f else info["frames"]
        n_src = max(0, end_eff - start_f)
        ratio = plan.out_rate / src_rate
        ram = s.ram_playback
        ring_note = None
        real_path, av_opts = track["path"], None
        chunk_in = 4096
        if ram:
            total_out = int(math.ceil(n_src * ratio)) + 16384
            if total_out * p.frame_bytes > s.ram_max_mb * 1024 * 1024:
                ram = False
                ring_note = f"RAM上限({s.ram_max_mb}MB)超過のためストリーミング再生"
            else:
                chunk_in = 32768
        out_chunk = int(math.ceil(chunk_in * ratio)) + 64
        if ram:
            capacity, ring, ready = total_out, False, 1 << 62
        else:
            capacity = max(int(s.stream_buffer_s * plan.out_rate), out_chunk * 4)
            ring = True
            ready = min(capacity // 2, int(0.25 * plan.out_rate))
        shm = SharedSource.create(capacity, p.frame_bytes, ring)
        self._sid += 1
        sid = self._sid
        shm.volume = 1.0 if p.kind == "alsa_hw" else self.volume
        dur_s = n_src / src_rate if src_rate else 0.0
        dur_total = ((end_eff - int(track.get("start_frame", 0))) / src_rate) if src_rate else 0.0
        sig = self._signal_path(plan, ram, ring_note)
        src = Source(sid, track, shm, plan, int(start_s * plan.out_rate), dur_total, ram, sig, ready)
        self.sources[sid] = src
        self.meta[sid] = {"track": track, "out_rate": plan.out_rate, "start_offset": src.start_offset,
                          "dur_s": dur_total, "signal": sig}
        if len(self.meta) > 12:
            for k in sorted(self.meta)[:-12]:
                self.meta.pop(k, None)
        job = {
            "job_id": sid, "shm_name": shm.name, "frame_bytes": p.frame_bytes,
            "path": real_path, "av_options": av_opts, "start_frame": start_f, "end_frame": end_f,
            "out_rate": plan.out_rate, "out_channels": p.channels, "fmt": p.fmt, "valid_bits": p.valid_bits,
            "dither": s.dither, "nshape": self._nshape_name(), "quality": s.resample_quality, "phase": s.resample_phase, "band": s.resample_band,
            "engine": self._engine_name(s), "r8_tb": self._r8p(s)[0], "r8_att": self._r8p(s)[1], "r8_phase": self._r8p(s)[2],
            "r8_headroom_db": s.r8_headroom_db,
            "chunk_in": chunk_in, "seed": sid * 7919, "prio": prio,
        }
        self.prod_conn.send(("add", job))
        return src

    @staticmethod
    def _engine_name(s) -> str:
        from . import r8brain
        return "r8brain" if s.resample_engine == "r8brain" and r8brain.available() else "soxr"

    @staticmethod
    def _r8p(s):
        from . import r8brain
        return r8brain.params(s.r8_preset, s.r8_trans_band, s.r8_atten_db, s.r8_phase)

    def _retire(self, sid: int):
        src = self.sources.pop(sid, None)
        if src is None:
            return
        try:
            self.prod_conn.send(("cancel", sid))
        except (OSError, BrokenPipeError):
            pass
        src.shm.close()
        src.shm.unlink()
        for k, v in list(self._ahead.items()):
            if v is src:
                self._ahead.pop(k, None)
        if self.cur_src is src:
            self.cur_src = None
        if self.next_src is src:
            self.next_src = None

    # ---- 他アプリの音声を取り込んで再生 ----
    def list_streams(self) -> list[dict]:
        return pwutil.list_app_streams(exclude_pids={os.getpid()})

    def _end_capture(self, notify: bool = False):
        cap, self._cap = self._cap, None
        if cap is None:
            return
        try:
            self.prod_conn.send(("cancel", cap["sid"]))
        except (OSError, BrokenPipeError):
            pass
        if cap.get("src") is not None and self.cur_src is cap["src"]:
            self.out.cmds.put(("stop",))
            self.cur_src = None
            self.state = "stopped"
        self._retire(cap["sid"])
        pw = cap.get("pw")
        pwutil.capture_end(pw)
        if pw:
            self._touched_apps |= set(pw.get("touched") or ())
        if notify:
            self.emit("capture_stopped")

    def _restore_apps(self, sink: str | None):
        """取り込みで出力先を動かしたアプリを、メイン機器のシンクへ戻す(共有へ戻した時に別デバイスへ行かないように)。"""
        keys, self._touched_apps = self._touched_apps, set()
        if keys and sink:
            n = pwutil.move_app_streams(keys, sink)
            if n:
                self.emit("notice", msg=f"アプリの出力先をメイン機器({sink})へ戻しました")

    def _restore_apps_later(self):
        """排他の解放後: メイン機器のシンクが現れるのを待ってから裏で戻す。"""
        if not self._touched_apps or self.test_mode:
            return
        idx = self._main_card()
        if idx is None:
            return

        def work():
            self._restore_apps(pwutil.wait_sink_for_card(idx))
        threading.Thread(target=work, daemon=True).start()

    def _on_live_stats(self, sid: int, st: dict):
        """取り込みの統計(1秒ごと)。遅延目標の自動調整: 溜まりが枯れそう/音切れ → 増やす、長く余裕 → 少しずつ減らす。"""
        cap = self._cap
        if not cap or cap.get("sid") != sid:
            return
        cap["stats"] = st
        s = self.settings
        if not s.capture_latency_auto:
            return
        now = time.monotonic()
        rate = cap["rate"]
        tgt_ms = cap["target_ms"]
        tgt = int(rate * tgt_ms / 1000.0)
        fmin = st.get("fill_min", tgt)
        new = tgt_ms
        if self.out.underruns > cap["underruns0"] or fmin < tgt * 0.25:
            cap["underruns0"] = self.out.underruns
            cap["calm_t"] = now
            if now - cap["adapt_t"] > 3.0:
                new = min(s.capture_latency_max_ms, tgt_ms * 1.5 + 5.0)
        elif now - cap["calm_t"] > 30.0 and now - cap["adapt_t"] > 10.0 and fmin > tgt * 0.6:
            new = max(s.capture_latency_min_ms, tgt_ms * 0.9)
        if abs(new - tgt_ms) >= 0.5:
            cap["target_ms"] = new
            cap["adapt_t"] = now
            try:
                self.prod_conn.send(("live_ctl", sid, {"target": int(rate * new / 1000.0)}))
            except (OSError, BrokenPipeError):
                pass
            self.emit("notice", msg=f"アプリ音声の遅延目標を {tgt_ms:.0f} → {new:.0f} ms に自動調整しました")

    def start_capture(self, sink_input, label: str = "アプリ", backend: str | None = None):
        s = self.settings
        self._end_capture()
        self._drop_prefetch()
        self._drop_ahead()
        self.next_track = None
        self.out.cmds.put(("stop",))
        self.cur_track = None
        if not self.test_mode and sys.platform == "win32":
            self.emit("error", msg="アプリ音声の取り込みは Windows 版では未対応です", id=None)
            return
        if not self.test_mode and s.backend == "pipewire":
            self.emit("error", msg="アプリ音声の取り込みは排他出力専用です(共有では PipeWire に戻るだけで意味がありません)。"
                                   "再生バーの出力プロファイルを「排他」にしてから選び直してください", id=None)
            return
        inputs = [int(x) for x in (sink_input if isinstance(sink_input, (list, tuple)) else [sink_input])]
        self._cap = {"sid": -1}              # _decide でリサンプル無効にするため先に立てる
        test_cmd = os.environ.get("MIYAMIZU_CAPTURE_CMD")
        # 取り込みレートの候補: アプリのストリームのレート(PipeWire 内の変換を避ける) → 設定値
        cands = [int(s.capture_rate)]
        app_rate = None
        if s.capture_rate_auto and not test_cmd:
            app_rate = pwutil.stream_rate(inputs[0])
            if app_rate and app_rate != cands[0]:
                cands.insert(0, app_rate)
        backend = backend or ("pw-cat" if s.capture_backend == "pw-cat" and shutil_which("pw-cat") else "parec")
        pw = None
        plan = None
        rate = cands[0]
        try:
            for i, rate in enumerate(cands):
                info = {"rate": rate, "channels": 2, "frames": 0, "is_int": True, "src_bits": 32, "codec": "アプリ音声"}
                if test_cmd:
                    import shlex
                    cmd = shlex.split(test_cmd)
                else:
                    if not shutil_which("parec") and backend == "parec":
                        raise EngineError("parec が見つかりません(pipewire-pulseaudio / pulseaudio-utils を入れてください)")
                    # 先にアプリを取り込み用シンクへ移す。排他で DAC を開く時(プロファイル off 等)に
                    # アプリのストリームが巻き込まれて切れたり移動したりするのを避けるため。
                    pw = pwutil.capture_begin(inputs, rate)
                    if pw is None:
                        raise EngineError("アプリの音声をキャプチャ用シンクへ移せませんでした(そのアプリは今音を出していますか?)")
                    cmd = pwutil.capture_cmd(backend, pw["monitor"], rate)
                plan = self._prepare_session(info)
                if plan.out_rate == rate and plan.out_ch == 2:
                    break
                if i + 1 < len(cands):          # アプリのレートを DAC が受けられない: 設定の取り込みレートで取り直す
                    pwutil.capture_end(pw)
                    pw = None
                    continue
                raise EngineError(f"出力デバイスが {rate_label(rate)}/2ch に対応していません。設定の取り込みレートを変えてください")
        except EngineError as e:
            self._cap = None
            pwutil.capture_end(pw)
            self.emit("error", msg=str(e), id=None)
            return
        p = plan.params
        target = int(rate * s.capture_latency_ms / 1000.0)
        capacity = max(int(rate * max(1.0, s.stream_buffer_s * 2)), target * 4)
        shm = SharedSource.create(capacity, p.frame_bytes, True)
        self._sid += 1
        sid = self._sid
        steps = []
        if p.valid_bits < 32:
            steps.append(f"{p.valid_bits}bit へ量子化" + self._quant_note())
        sig = {"source": f"アプリ音声「{label}」 {rate_label(rate)} 2ch(PipeWire経由)", "steps": steps,
               "output": f"{p.fmt}({p.valid_bits}bit有効) {rate_label(p.rate)} {p.channels}ch", "device": p.device,
               "backend": p.kind, "bitperfect": (p.kind == "alsa_hw" and not steps), "ram": False,
               "notes": ["ライブ入力: RAM再生・リサンプルは無効",
                         "アプリ側のクロックと出力機器のクロック差は、バッファの1フレーム調整で吸収します",
                         f"遅延 約{s.capture_latency_ms:.0f}ms(動画と同時に見る時は設定で短く)"]}
        track = {"id": "live", "path": f"capture://{label}", "title": f"🎧 {label}", "artist": "アプリ音声入力",
                 "album": "", "duration": 0.0, "start_frame": 0, "end_frame": None}
        src = Source(sid, track, shm, plan, 0, 0.0, False, sig, target)
        self.sources[sid] = src
        self.meta[sid] = {"track": track, "out_rate": plan.out_rate, "start_offset": 0, "dur_s": 0.0, "signal": sig}
        self._cap = {"sid": sid, "src": src, "pw": pw, "rate": rate, "app_rate": app_rate, "backend": backend,
                     "label": label, "inputs": inputs, "target_ms": float(s.capture_latency_ms), "stats": {},
                     "adapt_t": time.monotonic(), "calm_t": time.monotonic(), "underruns0": self.out.underruns}
        job = {"live": True, "job_id": sid, "shm_name": shm.name, "frame_bytes": p.frame_bytes, "out_channels": p.channels,
               "in_channels": 2, "fmt": p.fmt, "valid_bits": p.valid_bits, "dither": s.dither, "nshape": self._nshape_name(), "seed": sid * 7919,
               "target_frames": target, "cmd": cmd, "sync": s.capture_sync if s.capture_sync in ("ratio", "slip") else "slip",
               "rate": rate}
        self.prod_conn.send(("add", job))
        self.cur_src = src
        self.cur_track = track
        self.paused = False
        self.state = "playing"
        self.out.cmds.put(("play", src, False))
        self._cap["t0"] = time.monotonic()
        self._cap["guard_t"] = 0.0
        self._cap["checked"] = False
        self.emit("capture_started", label=label)

    # ---- 再生制御 ----
    def start_track(self, track: dict, start_s: float = 0.0, play: bool = True):
        self._end_capture(notify=True)
        self._drop_prefetch()
        try:
            info = self._probe_track(track)
        except (DecodeError, KeyError, OSError) as e:
            self.emit("error", msg=f"再生できません: {track.get('path')} ({e})", id=track.get("id"))
            self._after_failed_track()
            return
        try:
            plan = self._prepare_session(info)
        except EngineError as e:
            self.emit("error", msg=str(e), id=track.get("id"))
            self.out.cmds.put(("stop",))
            self.state = "stopped"
            return
        src = self._make_source(track, plan, start_s)
        self.cur_src = src
        self.cur_track = track
        self.paused = not play
        self.state = "paused" if not play else "playing"
        self.out.cmds.put(("play", src, not play))
        self.emit("need_next", id=track.get("id"))
        self._schedule_prefetch()

    def _after_failed_track(self):
        if self.next_track is not None:
            nt, self.next_track = self.next_track, None
            self.start_track(nt)
        else:
            self.out.cmds.put(("stop",))
            self.state = "stopped"
            self.emit("finished", id=None)

    def _drop_prefetch(self):
        if self.next_src is not None:
            self.out.cmds.put(("set_next", None))
            self.next_src = None

    def _schedule_prefetch(self):
        s = self.settings
        if not s.prefetch or self.next_track is None or self.cur_src is None or self.out.params is None:
            return
        if self.next_src is not None:
            return
        try:
            info = self._probe_track(self.next_track)
            caps = None
            if self.out.params.kind == "alsa_hw":
                caps = self._cached_caps
            rate, ch, resample, forced = self._decide(info, caps)
        except (DecodeError, EngineError, KeyError, OSError, AttributeError):
            return
        p = self.out.params
        if (rate, ch) != (p.rate, p.channels):
            return    # デバイスの再オープンが必要 → ギャップあり遷移(曲末で通常開始)
        plan = Plan(p, info, info["rate"], rate, ch, resample, forced)
        src = self._make_source(self.next_track, plan, 0.0)
        self.next_src = src
        self.out.cmds.put(("set_next", src))

    # ---- 実験: 事前変換(次曲より先の曲も再生前に変換して RAM に置く) ----
    def _ahead_enabled(self) -> bool:
        s = self.settings
        return bool(s.ram_playback and s.prefetch and s.prerender_ahead > 0)

    def set_ahead(self, tracks: list[dict]):
        self.ahead_tracks = list(tracks or [])
        keep = {self._tkey(t) for t in self.ahead_tracks[: max(0, self.settings.prerender_ahead)]} if self._ahead_enabled() else set()
        for k, src in list(self._ahead.items()):
            if k not in keep:
                self._ahead.pop(k, None)
                self._retire(src.id)
        self._schedule_ahead()

    def _drop_ahead(self):
        for k, src in list(self._ahead.items()):
            self._ahead.pop(k, None)
            self._retire(src.id)

    def _schedule_ahead(self):
        """先の曲を 1 曲ずつ事前変換する(再生中の曲と次曲が RAM に載り終わってから)。"""
        s = self.settings
        if not self._ahead_enabled() or self.cur_src is None or self.out.params is None or self.state == "stopped":
            return
        if any(not a.shm.done for a in self._ahead.values()):
            return
        for src in (self.cur_src, self.next_src):
            if src is not None and src.ram and not src.shm.done:
                return
        p = self.out.params
        used = sum(a.shm.capacity * a.shm.frame_bytes for a in self._ahead.values())
        budget = s.prerender_total_mb * 1024 * 1024 - used
        nk = self._tkey(self.next_track) if self.next_track else None
        for t in self.ahead_tracks[: s.prerender_ahead]:
            k = self._tkey(t)
            if k in self._ahead or k == nk:
                continue
            try:
                info = self._probe_track(t)
                caps = self._cached_caps if p.kind == "alsa_hw" else None
                rate, ch, resample, forced = self._decide(info, caps)
            except (DecodeError, EngineError, KeyError, OSError, AttributeError):
                continue
            if (rate, ch) != (p.rate, p.channels):
                break              # デバイスの再オープンが要る曲(レート違い)以降は先読みしない
            end = t.get("end_frame") or info["frames"]
            n_src = max(0, int(end) - int(t.get("start_frame") or 0))
            need = (int(math.ceil(n_src * rate / info["rate"])) + 16384) * p.frame_bytes
            if need > s.ram_max_mb * 1024 * 1024 or need > budget:
                break
            src = self._make_source(t, Plan(p, info, info["rate"], rate, ch, resample, forced), 0.0, prio=1)
            if not src.ram:
                self._retire(src.id)
                break
            self._ahead[k] = src
            break

    @property
    def _cached_caps(self) -> alsa.Caps | None:
        return getattr(self, "_caps_cache", None)

    def set_next_track(self, track: dict | None):
        self.next_track = track
        self._drop_prefetch()
        self._schedule_prefetch()

    def seek(self, pos_s: float):
        src, track = self.cur_src, self.cur_track
        if src is None or track is None:
            return
        pos_s = max(0.0, min(pos_s, max(0.0, src.dur_s - 0.05)))
        target = int(pos_s * src.out_rate)
        sh = src.shm
        if src.ram and target >= src.start_offset and (target - src.start_offset) <= sh.write_pos:
            self.out.cmds.put(("seek_local", target - src.start_offset))
            return
        self.start_track(track, pos_s, play=not self.paused)

    def set_volume(self, v: float):
        self.volume = max(0.0, min(1.0, float(v)))
        eff = 1.0 if self._kind() == "alsa_hw" else self.volume
        for src in self.sources.values():
            src.shm.volume = eff

    # ---- イベント処理 ----
    def _capture_watchdog(self):
        """取り込み開始から数秒たっても音が届かない時、原因の手掛かり付きで知らせる(無音のまま気付かないのを防ぐ)。"""
        cap = self._cap
        if not cap or cap.get("checked") or "t0" not in cap or time.monotonic() - cap["t0"] < 3.0:
            return
        cap["checked"] = True
        src = cap.get("src")
        try:
            wp = int(src.shm.write_pos) if src is not None else 0
        except Exception:  # noqa: BLE001
            wp = 0
        if wp > 0 or cap.get("pw") is None:
            return
        if cap.get("backend") == "pw-cat":          # pw-cat で届かない: parec へ自動で切り替える
            inputs, label = cap.get("inputs"), cap.get("label", "アプリ")
            self.emit("notice", msg="pw-cat では音が届かなかったため、parec に切り替えて取り込み直します")
            self._end_capture()
            self.start_capture(inputs, label, backend="parec")
            return
        where = ""
        for st in pwutil.list_app_streams():
            if st["index"] == cap["pw"]["sink_input"]:
                where = f" アプリの出力先は「{st.get('sink_name')}」です(取り込み用は {pwutil.CAPTURE_SINK})。"
        self.emit("error", msg="アプリの音声が届いていません。アプリが再生中か確認してください(一時停止中・別のストリームに切り替わった場合は選び直し)。"
                  + where, id=None)

    def _capture_guard(self):
        """取り込み中、選んだアプリ以外のストリームが取り込み用シンクに入ったら元の出力へ戻す(0.5秒ごと・裏で)。"""
        cap = self._cap
        if not cap or cap.get("pw") is None or getattr(self, "_guard_busy", False):
            return
        now = time.monotonic()
        if now - cap.get("guard_t", 0.0) < 0.5:
            return
        cap["guard_t"] = now
        self._guard_busy = True
        pw = cap["pw"]

        def work():
            try:
                moved = pwutil.capture_evict_others(pw)
                if moved:
                    self.events.put(("capture_evicted", sorted(set(moved))))
            finally:
                self._guard_busy = False
        threading.Thread(target=work, daemon=True).start()

    def _pump_events(self):
        self._capture_watchdog()
        self._capture_guard()
        while True:
            try:
                ev = self.events.get_nowait()
            except queue.Empty:
                break
            op = ev[0]
            if op == "retire":
                self._retire(ev[1])
            elif op == "source_end":
                pass
            elif op == "advanced":
                nid = ev[2]
                self.cur_src = self.sources.get(nid)
                self.cur_track = self.cur_src.track if self.cur_src else None
                self.next_src = None
                self.next_track = None
                self._retire(ev[1])
                self.emit("need_next", id=self.cur_track.get("id") if self.cur_track else None)
            elif op == "queue_empty":
                self._retire(ev[1])
                self._on_queue_empty()
            elif op == "capture_evicted":
                self.emit("notice", msg="選んだアプリ以外の音(" + ", ".join(ev[1]) + ")は取り込まず、元の出力へ戻しました")
            elif op == "streams_ready":
                if ev[1] in self.clients:
                    self.reply(ev[1], "streams", streams=ev[2])
            elif op == "device_closed":
                self._cleanup_device_state(keep_claim=True)
                self.emit("device_closed")
            elif op == "device_error":
                self.emit("error", msg=f"出力エラー: {ev[1]}", id=None)
                self.state = "stopped"
                self.cur_src = self.next_src = None
                self._cleanup_device_state()

    def _on_queue_empty(self):
        if self._cap is not None and self.cur_src is self._cap.get("src"):
            self._end_capture(notify=True)
            return
        self.cur_src = None
        if self.next_track is not None:
            nt, self.next_track = self.next_track, None
            self.start_track(nt)
            return
        self.state = "stopped"
        fin = self.cur_track.get("id") if self.cur_track else None
        self.emit("finished", id=fin)

    def _nshape_name(self) -> str:
        """有効なノイズシェーピング名(ライブラリが使えない時は off)。"""
        n = self.settings.noise_shaping
        if n in (None, "", "off"):
            return "off"
        try:
            from . import nshape

            if n in nshape.FILTERS and nshape.available():
                return n
        except Exception:  # noqa: BLE001
            pass
        return "off"

    def _quant_note(self) -> str:
        n = self._nshape_name()
        if n != "off":
            return f" (ノイズシェーピング: {n}" + (" + TPDFディザ)" if self.settings.dither else ")")
        return " (TPDFディザ)" if self.settings.dither else ""

    def _apply_quiet(self):
        """静音モード: 準備プロセスの暇な時の待機を長くする(再生中の裏方の動きを減らす)。"""
        s = self.settings
        ms = s.quiet_idle_ms if s.quiet_mode else 10.0
        try:
            if self.prod_conn is not None:
                self.prod_conn.send(("opt", {"idle_poll": ms / 1000.0}))
        except (OSError, BrokenPipeError):
            pass

    def _pump_producer(self):
        conn = self.prod_conn
        try:
            while conn.poll(0):
                msg = conn.recv()
                if msg[0] == "error":
                    src = self.sources.get(msg[1])
                    tid = src.track.get("id") if src else None
                    path = src.track.get("path") if src else ""
                    self.emit("error", msg=f"デコードエラー: {path} ({msg[2]})", id=tid)
                elif msg[0] == "live_stats":
                    self._on_live_stats(msg[1], msg[2])
        except (EOFError, OSError):
            if not self.quit:
                self.emit("error", msg="準備プロセスが終了しました。再起動します", id=None)
                self._start_producer()

    # ---- 状態 ----
    def _audible(self):
        out = self.out
        entries = tuple(out.timeline)
        if not entries:
            return None
        played = out.stream_pos - out.delay_frames
        chosen = None
        for e in entries:
            if e[0] <= played:
                chosen = e
            else:
                break
        tl = out.timeline
        try:
            while len(tl) > 2 and tl[1][0] <= played:
                tl.popleft()
        except IndexError:
            pass
        if chosen is None:
            return None
        b, sid, base, running = chosen
        frames = base + ((played - b) if running else 0)
        return sid, frames, running

    def _emit_status(self):
        a = self._audible()
        pos = 0.0
        dur = 0.0
        tid = None
        if a is not None:
            sid, frames, _ = a
            m = self.meta.get(sid)
            if m:
                pos = min(m["dur_s"], (m["start_offset"] + max(0, frames)) / m["out_rate"])
                dur = m["dur_s"]
                tid = m["track"].get("id")
                if sid != self.audible_id:
                    self.audible_id = sid
                    self.emit("track_changed", id=tid, track=m["track"], signal=m["signal"])
        elif self.cur_src is not None:
            m = self.meta.get(self.cur_src.id)
            if m:
                pos = m["start_offset"] / m["out_rate"]
                dur = m["dur_s"]
                tid = m["track"].get("id")
        state = self.state
        if state == "playing" and self.cur_src is None and a is None:
            state = "stopped"
        self.last_status = {"state": state, "id": tid, "pos": round(pos, 3), "dur": round(dur, 3),
                            "volume": self.volume}
        self.emit("status", **self.last_status)

    def _emit_metrics(self):
        snap = self.out.snapshot()
        freqs = sysinfo.read_freqs_mhz()
        ram = []
        for src, role in [(self.cur_src, "cur"), (self.next_src, "next")] + [(a, "ahead") for a in list(self._ahead.values())]:
            if src is not None and src.ram:
                sh = src.shm
                pct = 1.0 if sh.done else (sh.write_pos / sh.capacity if sh.capacity else 0.0)
                ram.append({"id": src.track.get("id"), "role": role, "done": bool(sh.done and not sh.error),
                            "pct": round(pct, 3), "mb": round(sh.capacity * sh.frame_bytes / 1048576, 1)})
        cap_ms = None
        if self._cap and self._cap.get("src") is not None:
            try:
                rate = self._cap["src"].plan.out_rate
                cap_ms = round((self._cap["src"].shm.available() + (snap.get("delay_frames") or 0)) / rate * 1000.0, 1)
            except Exception:  # noqa: BLE001
                cap_ms = None
        self.emit("metrics", cap_latency_ms=cap_ms, out=snap, freq=freqs, ram=ram, ded=sorted(self._ded), reserved=sorted(self._reserved), others=sorted(self._others),
                  producer_pid=self.prod_proc.pid if self.prod_proc else None)

    def release_device(self, reason: str = ""):
        """再生を止めて出力デバイスを閉じ、PipeWire に返す(OS のミキサーに DAC が戻る)。"""
        self._end_capture(notify=True)
        self._drop_prefetch()
        self._drop_ahead()
        self.out.cmds.put(("stop",))
        self.cur_track = None
        self.state = "stopped"
        self.paused = False
        self.audible_id = 0
        self._close_device()
        self._cleanup_device_state()
        self._restore_apps_later()
        self.emit("device_closed", reason=reason)

    # ---- コマンド ----
    def hello(self, c: Client):
        devs = [{"name": d.name, "label": d.label, "card_index": d.card_index} for d in alsa.list_hw_devices()]
        self.reply(c, "hello", version=__version__, pid=os.getpid(), devices=devs, state=self.state,
                   cpu={"topology": sysinfo.topology(), "freq": sysinfo.cpufreq_info()},
                   settings=self.settings.to_dict(), test=self.test_mode, libsoxr=soxr_ctypes.available(), r8brain=__import__("miyamizu.r8brain", fromlist=["x"]).available(),
                   assignment=self.assignment)

    def handle(self, c: Client, msg: dict):
        cmd = msg.get("cmd")
        try:
            if cmd == "hello":
                self.hello(c)
            elif cmd == "configure":
                old = self.settings
                self.settings = Settings.from_dict(msg["settings"])
                if sys.platform == "win32":       # PipeWire/ALSA 専用の項目は Windows では使わない
                    self.settings = replace(self.settings, release_pipewire=False, release_profile_off=False,
                                            claim_on_switch=False, default_on_shared=False, pw_force_rate=False)
                if (old.backend, old.alsa_device, old.custom_device) != (
                        self.settings.backend, self.settings.alsa_device, self.settings.custom_device) \
                        and (self.out.backend is not None or self._suspended or self._susp_cards or self._profiles_off):
                    # 出力先を切り替えた: 旧デバイスを即座に解放する(共有へ切替で排他を残さない)
                    self.release_device("出力設定の変更")
                self._drop_ahead()              # 設定が変わったら変換済みの先読みは作り直す
                self._apply_quiet()
                self.apply_assignment()
                self.apply_cpu_plan()
                self.set_volume(self.settings.volume if "volume" in msg["settings"] else self.volume)
            elif cmd == "load":
                self.start_track(msg["track"], float(msg.get("start", 0.0)), bool(msg.get("play", True)))
            elif cmd == "set_next":
                self.set_next_track(msg.get("track"))
            elif cmd == "pause":
                self.paused = True
                if self.state == "playing":
                    self.state = "paused"
                self.out.cmds.put(("pause", True))
            elif cmd == "resume":
                self.paused = False
                if self.state == "paused":
                    self.state = "playing"
                self.out.cmds.put(("pause", False))
            elif cmd == "list_streams":
                if not getattr(self, "_streams_busy", False):       # 音量測定に少し掛かるので裏で行う
                    self._streams_busy = True

                    def work(client=c):
                        try:
                            st = self.list_streams()
                            pwutil.probe_levels(st)
                            self.events.put(("streams_ready", client, st))
                        finally:
                            self._streams_busy = False
                    threading.Thread(target=work, daemon=True).start()
            elif cmd == "capture_start":
                self.start_capture(int(msg["sink_input"]), str(msg.get("label") or "アプリ"))
            elif cmd == "capture_stop":
                self._end_capture(notify=True)
            elif cmd == "stop":
                self._end_capture(notify=True)
                self._drop_prefetch()
                self._drop_ahead()
                self.out.cmds.put(("stop",))
                self.cur_track = None
                self.state = "stopped"
                self.paused = False
                self.audible_id = 0
            elif cmd == "seek":
                self.seek(float(msg["pos"]))
            elif cmd == "volume":
                self.set_volume(float(msg["v"]))
            elif cmd == "probe_device":
                caps = alsa.probe_caps(msg["device"])
                info = ""
                for d in alsa.list_hw_devices():
                    if d.name == msg["device"]:
                        info = alsa.usb_stream_info(d.card_index)
                self.reply(c, "device_caps", device=msg["device"], ok=caps.ok, error=caps.error, busy=caps.busy,
                           rates=caps.rates, formats=caps.formats, ch_min=caps.ch_min, ch_max=caps.ch_max,
                           stream_info=info)
            elif cmd == "set_ahead":
                self.set_ahead(msg.get("tracks") or [])
            elif cmd == "release_device":
                self.release_device("手動解放")
            elif cmd == "quit":
                self.quit = True
        except EngineError as e:
            self.emit("error", msg=str(e), id=None)
        except Exception as e:  # noqa: BLE001
            traceback.print_exc()
            self.emit("error", msg=f"エンジン内部エラー: {type(e).__name__}: {e}", id=None)

    # ---- サーバー ----
    def serve(self):
        try:
            os.unlink(self.sock_path)
        except FileNotFoundError:
            pass
        self.token = ""
        if use_tcp():           # Windows(と MIYAMIZU_TCP=1): 127.0.0.1 の TCP。ポートとトークンをファイルへ書く
            import secrets
            srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            srv.bind(("127.0.0.1", 0))
            self.token = secrets.token_hex(16)
            tmp = self.sock_path + ".tmp"
            with open(tmp, "w") as f:
                json.dump({"port": srv.getsockname()[1], "token": self.token, "pid": os.getpid()}, f)
            os.replace(tmp, self.sock_path)
        else:
            srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            srv.bind(self.sock_path)
            os.chmod(self.sock_path, 0o600)
        srv.listen(4)
        srv.setblocking(False)
        self.sel.register(srv, selectors.EVENT_READ, None)
        self.out.start()
        self.apply_cpu_plan()
        last_status = last_metrics = last_ahead = 0.0
        try:
            while not self.quit:
                quiet = bool(self.settings.quiet_mode) and self.state == "playing"
                for key, _ in self.sel.select(timeout=0.1 if quiet else 0.02):
                    if key.data is None:
                        conn, _ = srv.accept()
                        conn.setblocking(True)
                        cl = Client(conn, authed=not self.token)
                        self.clients.append(cl)
                        self.sel.register(conn, selectors.EVENT_READ, cl)
                    else:
                        self._read_client(key.data)
                self._pump_events()
                self._pump_producer()
                now = time.monotonic()
                if now - last_status >= (max(0.1, self.settings.quiet_status_s) if quiet else 0.1):
                    last_status = now
                    self._emit_status()
                if now - last_metrics >= (max(1.0, self.settings.quiet_metrics_s) if quiet else 1.0):
                    last_metrics = now
                    self._emit_metrics()
                if now - last_ahead >= 1.0:
                    last_ahead = now
                    self._schedule_ahead()
        finally:
            self.shutdown()
            try:
                srv.close()
                os.unlink(self.sock_path)
            except OSError:
                pass

    def _read_client(self, c: Client):
        try:
            c.sock.settimeout(0.2)
            data = c.sock.recv(65536)
        except (socket.timeout, BlockingIOError):
            return
        except OSError:
            data = b""
        if not data:
            self._drop_client(c)
            return
        c.buf += data
        while b"\n" in c.buf:
            line, c.buf = c.buf.split(b"\n", 1)
            if not line.strip():
                continue
            try:
                msg = json.loads(line)
            except ValueError:
                continue
            if not c.authed:
                if isinstance(msg, dict) and msg.get("cmd") == "auth" and msg.get("token") == self.token:
                    c.authed = True
                    continue
                self._drop_client(c)
                return
            self.handle(c, msg)

    def shutdown(self):
        self._end_capture()
        self.out.cmds.put(("quit",))
        self.out.join(timeout=3)
        for sid in list(self.sources):
            self._retire(sid)
        try:
            self.prod_conn.send(("quit",))
        except (OSError, BrokenPipeError):
            pass
        if self.prod_proc is not None:
            self.prod_proc.join(timeout=2)
            if self.prod_proc.is_alive():
                self.prod_proc.terminate()
        self._cleanup_device_state()
        for c in list(self.clients):
            self._drop_client(c)


def main(argv=None):
    ap = argparse.ArgumentParser(description="Miyamizu 再生エンジン")
    ap.add_argument("--socket", default=None)
    ap.add_argument("--test-backend", default=None, help="null | capture[:file]")
    ap.add_argument("--test-format", default="S32_LE")
    ap.add_argument("--no-rt", action="store_true")
    a = ap.parse_args(argv)
    try:                                    # 致命的エラー(セグフォ等)の調査用にデータフォルダへ残す
        import faulthandler

        from .common import data_dir
        d = data_dir()
        d.mkdir(parents=True, exist_ok=True)
        faulthandler.enable(file=open(d / "engine_crash.log", "a", buffering=1), all_threads=True)
    except Exception:  # noqa: BLE001
        pass
    eng = Engine(a.socket, a.test_backend, a.test_format, enable_rt=not a.no_rt)
    signal.signal(signal.SIGTERM, lambda *_: setattr(eng, "quit", True))
    signal.signal(signal.SIGINT, lambda *_: setattr(eng, "quit", True))
    eng.serve()


if __name__ == "__main__":
    main()
