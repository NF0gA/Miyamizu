"""PipeWire 補助(best-effort)。コマンドが無い/失敗しても再生は続ける。"""
from __future__ import annotations

import json
import shutil
import subprocess
import sys


def _run(cmd: list[str], timeout: float = 3.0) -> subprocess.CompletedProcess | None:
    if sys.platform == "win32":          # PipeWire/pactl は無い
        return None
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.SubprocessError):
        return None


def set_force_rate(rate: int) -> bool:
    """PipeWire グラフのクロックを一時的に rate に固定する。0 で解除。"""
    if not shutil.which("pw-metadata"):
        return False
    r = _run(["pw-metadata", "-n", "settings", "0", "clock.force-rate", str(int(rate))])
    return bool(r and r.returncode == 0)


def suspend_sinks_for_card(card_index: int) -> list[str]:
    """ALSA カードに対応する PipeWire シンクを suspend し、デバイスを解放させる。名前の一覧を返す。"""
    if not shutil.which("pactl"):
        return []
    r = _run(["pactl", "--format=json", "list", "sinks"])
    if not r or r.returncode != 0:
        return []
    try:
        sinks = json.loads(r.stdout)
    except ValueError:
        return []
    done: list[str] = []
    for s in sinks:
        props = s.get("properties", {}) or {}
        if str(props.get("alsa.card", "")) == str(card_index):
            name = s.get("name")
            if name:
                rr = _run(["pactl", "suspend-sink", name, "1"])
                if rr and rr.returncode == 0:
                    done.append(name)
    return done


def resume_sinks(names: list[str]) -> None:
    if not shutil.which("pactl"):
        return
    for n in names:
        _run(["pactl", "suspend-sink", n, "0"])


def resume_card(card_index: int) -> None:
    """ALSA カードに対応する全シンクの suspend を解除する(名前を覚えていなくても使える)。"""
    if not shutil.which("pactl"):
        return
    r = _run(["pactl", "--format=json", "list", "sinks"])
    if not r or r.returncode != 0:
        return
    try:
        sinks = json.loads(r.stdout)
    except ValueError:
        return
    for s in sinks:
        if str((s.get("properties") or {}).get("alsa.card", "")) == str(card_index) and s.get("name"):
            _run(["pactl", "suspend-sink", s["name"], "0"])


# ---------------------------------------------------------------- カードのプロファイル(確実に解放する最終手段)
def _list_cards() -> list[dict]:
    if not shutil.which("pactl"):
        return []
    r = _run(["pactl", "--format=json", "list", "cards"])
    if not r or r.returncode != 0:
        return []
    try:
        return json.loads(r.stdout)
    except ValueError:
        return []


def find_card(card_index: int) -> dict | None:
    """ALSA カード番号に対応する PipeWire/Pulse カード {name, active_profile}。無ければ None。"""
    for c in _list_cards():
        if str((c.get("properties") or {}).get("alsa.card", "")) == str(card_index):
            return {"name": c.get("name"), "active_profile": c.get("active_profile")}
    return None


def set_card_profile(name: str, profile: str) -> bool:
    r = _run(["pactl", "set-card-profile", name, profile], timeout=5.0)
    return bool(r and r.returncode == 0)


def pcm_owners(card_index: int) -> list[str]:
    """ALSA カードを今開いているプロセス名(例: 'wireplumber(1234)')。/proc/asound の owner_pid から調べる。"""
    import glob
    import os

    out: list[str] = []
    for f in glob.glob(f"/proc/asound/card{card_index}/pcm*/sub*/status"):
        try:
            txt = open(f).read()
        except OSError:
            continue
        for line in txt.splitlines():
            if line.startswith("owner_pid"):
                try:
                    pid = int(line.split(":")[1])
                except (ValueError, IndexError):
                    continue
                try:
                    comm = open(f"/proc/{pid}/comm").read().strip()
                except OSError:
                    comm = "?"
                label = f"{comm}({pid})"
                if label not in out and pid != os.getpid():
                    out.append(label)
    return out


# ---------------------------------------------------------------- 共有(PipeWire)側の割り当て
def list_sinks() -> list[dict]:
    if not shutil.which("pactl"):
        return []
    r = _run(["pactl", "--format=json", "list", "sinks"])
    if not r or r.returncode != 0:
        return []
    try:
        return json.loads(r.stdout)
    except ValueError:
        return []


def sink_for_card(card_index: int) -> str | None:
    """ALSA カード番号に対応する PipeWire シンク名。無ければ None。"""
    for s in list_sinks():
        if str((s.get("properties") or {}).get("alsa.card", "")) == str(card_index) and s.get("name"):
            return s["name"]
    return None


def wait_sink_for_card(card_index: int, timeout: float = 4.0) -> str | None:
    """プロファイル復元直後はシンクが現れるまで少し掛かるので待つ。"""
    import time

    end = time.monotonic() + timeout
    while True:
        n = sink_for_card(card_index)
        if n or time.monotonic() >= end:
            return n
        time.sleep(0.25)


def get_default_sink() -> str | None:
    if not shutil.which("pactl"):
        return None
    r = _run(["pactl", "get-default-sink"])
    if r and r.returncode == 0 and r.stdout.strip():
        return r.stdout.strip()
    return None


def set_default_sink(name: str) -> bool:
    r = _run(["pactl", "set-default-sink", name])
    return bool(r and r.returncode == 0)


# ---------------------------------------------------------------- 他アプリの音声を取り込む(キャプチャ入力)
CAPTURE_SINK = "miyamizu_capture"


def list_app_streams(exclude_pids: set[int] | None = None) -> list[dict]:
    """今音を出しているアプリのストリーム [{index, app, media, binary, pid, sink_name, corked}]。"""
    if not shutil.which("pactl"):
        return []
    r = _run(["pactl", "--format=json", "list", "sink-inputs"])
    if not r or r.returncode != 0:
        return []
    try:
        items = json.loads(r.stdout)
    except ValueError:
        return []
    sinks = {s.get("index"): s.get("name") for s in list_sinks()}
    out = []
    for it in items:
        pr = it.get("properties") or {}
        try:
            pid = int(pr.get("application.process.id", 0))
        except (TypeError, ValueError):
            pid = 0
        if exclude_pids and pid in exclude_pids:
            continue
        out.append({"index": it.get("index"), "app": pr.get("application.name") or "?",
                    "media": pr.get("media.name") or "", "binary": pr.get("application.process.binary") or "",
                    "icon": pr.get("application.icon_name") or "", "pid": pid, "sink_name": sinks.get(it.get("sink")), "corked": bool(it.get("corked"))})
    return out


def _spec_rate(it: dict) -> int | None:
    import re

    m = re.search(r"(\d+)\s*Hz", str(it.get("sample_specification") or ""))
    return int(m.group(1)) if m else None


def stream_rate(sink_input: int) -> int | None:
    """ストリームのサンプルレート(sample_specification の '... 44100Hz')。"""
    for it in _sink_inputs():
        if it.get("index") == int(sink_input):
            return _spec_rate(it)
    return None


def capture_cmd(backend: str, monitor: str, rate: int) -> list[str]:
    """取り込み用のコマンド(raw s32le・2ch を stdout へ)。pw-cat は PipeWire ネイティブ(実験)。"""
    if backend == "pw-cat":
        node = monitor[:-len(".monitor")] if monitor.endswith(".monitor") else monitor
        return ["pw-cat", "--record", "--target", node, "-P", "stream.capture.sink=true", "--rate", str(rate),
                "--channels", "2", "--format", "s32", "--latency", "20ms", "--raw", "-"]
    return ["parec", f"--device={monitor}", "--format=s32le", f"--rate={rate}", "--channels=2",
            "--raw", "--latency-msec=20", "--client-name=Miyamizu-capture"]


def capture_begin(sink_input, rate: int, channels: int = 2) -> dict | None:
    """null シンクを作り、指定アプリのストリームをそこへ移す。モニターを録れば選んだアプリの音だけが取れる。"""
    if not shutil.which("pactl"):
        return None
    inputs = [int(x) for x in (sink_input if isinstance(sink_input, (list, tuple)) else [sink_input])]
    capture_cleanup_stale()
    prev = None
    keys = set()
    for it in _sink_inputs():
        if it.get("index") in inputs:
            keys.add(_ident(it.get("properties") or {}))
    for s in list_app_streams():
        if s["index"] == inputs[0]:
            prev = s.get("sink_name")
    cm = "front-left,front-right" if channels == 2 else "mono"
    r = _run(["pactl", "load-module", "module-null-sink", f"sink_name={CAPTURE_SINK}", f"rate={int(rate)}",
              f"channels={channels}", f"channel_map={cm}",
              "sink_properties=device.description=Miyamizu-Capture"], timeout=5.0)
    if not r or r.returncode != 0 or not r.stdout.strip().isdigit():
        return None
    mod = int(r.stdout.strip())
    ok = []
    for i in inputs:
        rr = _run(["pactl", "move-sink-input", str(i), CAPTURE_SINK])
        if rr and rr.returncode == 0:
            ok.append(i)
    if not ok:
        _run(["pactl", "unload-module", str(mod)])
        return None
    keys = {k for k in keys if k != ("", "")} or set(keys)
    return {"module": mod, "sink_input": ok[0], "inputs": ok, "prev_sink": prev, "monitor": f"{CAPTURE_SINK}.monitor",
            "keys": keys, "key": next(iter(keys), None), "vols": {}, "touched": set(keys)}


def _keys_of(state: dict) -> set:
    ks = set(state.get("keys") or ())
    if state.get("key") is not None:
        ks.add(state["key"])
    return ks


def capture_end(state: dict | None, dest: str | None = None) -> None:
    """アプリのストリームを元(または dest)のシンクへ戻し、null シンクを消す。
    元のシンクが今ない(排他で DAC を外している等)時は既定の出力へ戻す。"""
    if not state or not shutil.which("pactl"):
        return
    if not dest or not any(s.get("name") == dest for s in list_sinks()):
        dest = state.get("prev_sink")
    if not dest or not any(s.get("name") == dest for s in list_sinks()):
        dest = get_default_sink()
    if dest and dest != CAPTURE_SINK:
        keys = set(state.get("touched") or ())
        if keys:
            move_app_streams(keys, dest)
        else:
            _run(["pactl", "move-sink-input", str(state["sink_input"]), dest])
    _run(["pactl", "unload-module", str(state["module"])])


def capture_cleanup_stale() -> None:
    """異常終了で残った null シンクを片付ける(アプリの音が消えたままにならないように)。"""
    if not shutil.which("pactl"):
        return
    r = _run(["pactl", "list", "short", "modules"])
    if not r or r.returncode != 0:
        return
    mods = [ln.split("\t") for ln in r.stdout.splitlines() if CAPTURE_SINK in ln]
    if not mods:
        return
    dest = get_default_sink()
    if dest == CAPTURE_SINK:
        dest = None
    sink_idx = None
    for s in list_sinks():
        if s.get("name") == CAPTURE_SINK:
            sink_idx = s.get("index")
    if dest and sink_idx is not None:
        rr = _run(["pactl", "--format=json", "list", "sink-inputs"])
        try:
            for it in json.loads(rr.stdout) if rr and rr.returncode == 0 else []:
                if it.get("sink") == sink_idx:
                    _run(["pactl", "move-sink-input", str(it.get("index")), dest])
        except ValueError:
            pass
    for m in mods:
        _run(["pactl", "unload-module", m[0]])


def peak_db(raw: bytes) -> float | None:
    """s16le モノラル生データのピーク(dBFS)。データが無ければ None。"""
    import math

    n = len(raw) // 2
    if n == 0:
        return None
    import array

    a = array.array("h")
    a.frombytes(raw[:n * 2])
    pk = max(max(a), -min(a)) if len(a) else 0
    return -120.0 if pk == 0 else 20.0 * math.log10(pk / 32768.0)


def probe_levels(streams: list[dict], seconds: float = 0.3, cmd_for=None) -> None:
    """各ストリームを実際に短く聴いて、今音が出ているか(level_db / active)を調べる。streams を直接更新する。
    parec の --monitor-stream で、そのストリームだけをシンクのモニターから取る(音には影響しない)。"""
    import subprocess
    import threading
    import time

    if not (cmd_for or shutil.which("parec")):
        return

    def default_cmd(st):
        return ["parec", f"--device={st['sink_name']}.monitor", f"--monitor-stream={st['index']}", "--format=s16le",
                "--rate=8000", "--channels=1", "--raw", "--latency-msec=20", "--client-name=Miyamizu-probe"]

    def work(st):
        st["level_db"], st["active"] = None, False
        if not st.get("sink_name") or st.get("corked"):
            return
        try:
            p = subprocess.Popen((cmd_for or default_cmd)(st), stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, bufsize=0)
        except OSError:
            return
        buf = b""
        end = time.monotonic() + seconds
        try:
            import os
            import select

            while time.monotonic() < end:
                r, _, _ = select.select([p.stdout], [], [], max(0.0, end - time.monotonic()))
                if r:
                    chunk = os.read(p.stdout.fileno(), 65536)
                    if not chunk:
                        break
                    buf += chunk
        finally:
            try:
                p.kill()
                p.wait(timeout=1)
            except Exception:  # noqa: BLE001
                pass
        db = peak_db(buf)
        st["level_db"] = db
        st["active"] = db is not None and db > -70.0

    ths = [threading.Thread(target=work, args=(st,), daemon=True) for st in streams]
    for t in ths:
        t.start()
    for t in ths:
        t.join(seconds + 2.0)


def _ident(props: dict) -> tuple[str, str]:
    """ストリームの持ち主の識別(インデックスはシークや再生し直しで変わるので、アプリ名+実行ファイルで見る)。"""
    return (props.get("application.name") or "", props.get("application.process.binary") or "")


def _sink_inputs() -> list[dict]:
    r = _run(["pactl", "--format=json", "list", "sink-inputs"])
    if not r or r.returncode != 0:
        return []
    try:
        return json.loads(r.stdout)
    except ValueError:
        return []


def move_app_streams(keys, sink_name: str | None) -> int:
    """指定したアプリ(識別キーの集合)のストリームをすべて sink_name へ移す。移した数を返す。
    (move-sink-input はストリームの出力先を固定するため、取り込みの後始末で使うメインのシンクへ戻す)"""
    if not keys or not sink_name or not shutil.which("pactl"):
        return 0
    n = 0
    sinks = {s.get("index"): s.get("name") for s in list_sinks()}
    for it in _sink_inputs():
        if _ident(it.get("properties") or {}) in keys and sinks.get(it.get("sink")) != sink_name:
            rr = _run(["pactl", "move-sink-input", str(it.get("index")), sink_name])
            if rr and rr.returncode == 0:
                n += 1
    return n


def capture_evict_others(state: dict | None) -> list[str]:
    """取り込み中の整理(0.5秒ごと)。
    ・選んだアプリ自身の新しいストリーム(シークや再生し直しで作り直される)は取り込み用シンクへ追従させる
    ・選んだアプリ以外のストリームが取り込み用シンクに紛れ込んでいたら元の出力へ戻す(戻したアプリ名を返す)
    (PipeWire は前回の出力先を覚えていて、以前に取り込んだアプリの新しいストリームを取り込み用シンクへ戻すことがある)"""
    if not state or not shutil.which("pactl"):
        return []
    sink_idx = None
    sinks = list_sinks()
    for s in sinks:
        if s.get("name") == CAPTURE_SINK:
            sink_idx = s.get("index")
    if sink_idx is None:
        return []
    items = _sink_inputs()
    keys = _keys_of(state)
    inputs = set(state.get("inputs") or ()) | ({state["sink_input"]} if state.get("sink_input") is not None else set())
    dest = state.get("prev_sink")
    if not dest or dest == CAPTURE_SINK or not any(s.get("name") == dest for s in sinks):
        dest = get_default_sink()
    moved = []
    touched = state.setdefault("touched", set())
    vols = state.get("vols") or {}
    for it in items:
        ident = _ident(it.get("properties") or {})
        on_cap = it.get("sink") == sink_idx
        if ident in keys or it.get("index") in inputs:
            touched.add(ident)
            if not on_cap:                  # 選んだアプリの作り直されたストリームは追従させる
                rr = _run(["pactl", "move-sink-input", str(it.get("index")), CAPTURE_SINK])
                if rr and rr.returncode == 0:
                    inputs.add(int(it.get("index")))
                    state["sink_input"] = int(it.get("index"))
                    if ident in vols:
                        _run(["pactl", "set-sink-input-volume", str(it.get("index")), f"{vols[ident]}%"])
            continue
        if on_cap and dest and dest != CAPTURE_SINK:
            rr = _run(["pactl", "move-sink-input", str(it.get("index")), dest])
            if rr and rr.returncode == 0:
                touched.add(ident)
                moved.append(ident[0] or "?")
    state["inputs"] = list(inputs)
    return moved
