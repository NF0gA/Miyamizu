"""CPU トポロジ・周波数・アフィニティ・リアルタイム優先度まわり。"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import threading
from pathlib import Path

IS_WIN = sys.platform == "win32"
if not IS_WIN:
    import resource

SYS_CPU = Path("/sys/devices/system/cpu")


def _universe() -> frozenset[int]:
    """使える CPU 全体。起動時の affinity が GUI に絞られていた場合でも専用コアを見失わないよう、
    まず /sys の online 一覧を使い(取れなければ affinity)、cgroup の cpuset があれば交差させる。"""
    cpus: set[int] = set()
    try:
        txt = (SYS_CPU / "online").read_text().strip()
        cpus = set(parse_cpu_list(txt)) if txt else set()
    except (OSError, ValueError):
        cpus = set()
    if not cpus:
        try:
            cpus = set(os.sched_getaffinity(0))
        except (AttributeError, OSError):
            cpus = set(range(os.cpu_count() or 1))
    for f in ("/sys/fs/cgroup/cpuset.cpus.effective",):
        try:
            allowed = set(parse_cpu_list(Path(f).read_text().strip()))
            if allowed and cpus & allowed:
                cpus &= allowed
        except (OSError, ValueError):
            pass
    return frozenset(cpus)


_UNIVERSE: frozenset[int] | None = None


def online_cpus() -> list[int]:
    """使える CPU 全体(現在のスレッドの affinity ではなく、システム全体の許可集合)。"""
    global _UNIVERSE
    if _UNIVERSE is None:
        _UNIVERSE = _universe()
    return sorted(_UNIVERSE)


def _read(p: Path) -> str | None:
    try:
        return p.read_text().strip()
    except OSError:
        return None


def parse_cpu_list(s: str) -> list[int]:
    out: list[int] = []
    for part in s.split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            a, b = part.split("-", 1)
            out.extend(range(int(a), int(b) + 1))
        else:
            out.append(int(part))
    return out


_WIN_SIB: dict[int, list[int]] | None = None


def _win_siblings() -> dict[int, list[int]]:
    """Windows: GetLogicalProcessorInformation の RelationProcessorCore から SMT 兄弟を得る(取れなければ空)。"""
    global _WIN_SIB
    if _WIN_SIB is not None:
        return _WIN_SIB
    out: dict[int, list[int]] = {}
    try:
        import ctypes
        from ctypes import wintypes

        class SLPI(ctypes.Structure):
            _fields_ = [("mask", ctypes.c_size_t), ("rel", wintypes.DWORD), ("u", ctypes.c_ubyte * 16)]
        k32 = ctypes.windll.kernel32
        n = wintypes.DWORD(0)
        k32.GetLogicalProcessorInformation(None, ctypes.byref(n))
        cnt = n.value // ctypes.sizeof(SLPI)
        arr = (SLPI * cnt)()
        if k32.GetLogicalProcessorInformation(arr, ctypes.byref(n)):
            for e in arr:
                if e.rel == 0:      # RelationProcessorCore
                    cpus = [i for i in range(64) if e.mask >> i & 1]
                    for c in cpus:
                        out[c] = cpus
    except Exception:  # noqa: BLE001
        out = {}
    _WIN_SIB = out
    return out


def siblings(cpu: int) -> list[int]:
    if IS_WIN:
        return _win_siblings().get(cpu, [cpu])
    s = _read(SYS_CPU / f"cpu{cpu}" / "topology" / "thread_siblings_list")
    if not s:
        return [cpu]
    try:
        return parse_cpu_list(s)
    except ValueError:
        return [cpu]


def topology() -> list[dict]:
    """[{cpu, core_id, siblings}]"""
    res = []
    for c in online_cpus():
        cid = _read(SYS_CPU / f"cpu{c}" / "topology" / "core_id")
        res.append({"cpu": c, "core_id": int(cid) if cid and cid.isdigit() else c, "siblings": siblings(c)})
    return res


def read_freqs_mhz() -> dict[int, float]:
    out: dict[int, float] = {}
    for c in online_cpus():
        v = _read(SYS_CPU / f"cpu{c}" / "cpufreq" / "scaling_cur_freq")
        if v and v.isdigit():
            out[c] = int(v) / 1000.0
    if out:
        return out
    try:
        idx = -1
        for line in Path("/proc/cpuinfo").read_text().splitlines():
            if line.startswith("processor"):
                idx = int(line.split(":")[1])
            elif line.startswith("cpu MHz") and idx >= 0:
                out[idx] = float(line.split(":")[1])
    except (OSError, ValueError):
        pass
    return out


def cpufreq_info() -> dict:
    d = SYS_CPU / "cpu0" / "cpufreq"
    return {
        "driver": _read(d / "scaling_driver"),
        "governor": _read(d / "scaling_governor"),
        "epp": _read(d / "energy_performance_preference"),
        "boost": _read(SYS_CPU / "cpufreq" / "boost"),
    }


def plan_cpus(dedicated: int, reserve_sibling: bool) -> tuple[set[int], set[int]]:
    """(専用に使う CPU 集合, それ以外の処理が使う CPU 集合)。専用なしなら (空, 全CPU)。"""
    allc = set(online_cpus())
    if dedicated < 0 or dedicated not in allc:
        return set(), allc
    ded = {dedicated}
    if reserve_sibling:
        ded |= {s for s in siblings(dedicated) if s in allc}
    others = allc - ded
    if not others:     # 他に使える CPU が無い場合は分離しない
        return set(), allc
    return {dedicated}, others


def reserved_siblings(dedicated: int, reserve_sibling: bool) -> set[int]:
    """専用コアの SMT 兄弟(空けておくだけで使わない CPU)。"""
    if dedicated < 0 or dedicated not in set(online_cpus()) or not reserve_sibling:
        return set()
    return {c for c in siblings(dedicated) if c != dedicated and c in set(online_cpus())}


def _win_mask(cpus: set[int]) -> int:
    m = 0
    for c in cpus:
        if 0 <= c < 64:
            m |= 1 << c
    return m


def set_thread_affinity(cpus: set[int]) -> bool:
    if IS_WIN:
        try:
            import ctypes
            k32 = ctypes.windll.kernel32
            k32.GetCurrentThread.restype = ctypes.c_void_p
            k32.SetThreadAffinityMask.argtypes = [ctypes.c_void_p, ctypes.c_size_t]
            k32.SetThreadAffinityMask.restype = ctypes.c_size_t
            return bool(_win_mask(cpus)) and k32.SetThreadAffinityMask(k32.GetCurrentThread(), _win_mask(cpus)) != 0
        except Exception:  # noqa: BLE001
            return False
    try:
        os.sched_setaffinity(0, cpus)   # pid=0 は「呼び出したスレッド」(Linux)
        return True
    except (AttributeError, OSError):
        return False


def confine_process(cpus: set[int]) -> bool:
    """プロセス内の全スレッドを cpus に制限する。"""
    if IS_WIN:
        try:
            import ctypes
            k32 = ctypes.windll.kernel32
            k32.GetCurrentProcess.restype = ctypes.c_void_p
            k32.SetProcessAffinityMask.argtypes = [ctypes.c_void_p, ctypes.c_size_t]
            return bool(_win_mask(cpus)) and bool(k32.SetProcessAffinityMask(k32.GetCurrentProcess(), _win_mask(cpus)))
        except Exception:  # noqa: BLE001
            return False
    ok = False
    try:
        for t in os.listdir("/proc/self/task"):
            try:
                os.sched_setaffinity(int(t), cpus)
                ok = True
            except OSError:
                pass
    except OSError:
        try:
            os.sched_setaffinity(0, cpus)
            ok = True
        except (AttributeError, OSError):
            pass
    return ok


def rt_diagnosis() -> str:
    """RT が取れない時の原因の手掛かり(RLIMIT_RTPRIO、rtkit の有無など)。"""
    try:
        soft, hard = resource.getrlimit(resource.RLIMIT_RTPRIO)
    except (ValueError, OSError):
        soft = hard = -1
    fmt = lambda v: "unlimited" if v == resource.RLIM_INFINITY else str(v)  # noqa: E731
    parts = [f"RLIMIT_RTPRIO={fmt(soft)}/{fmt(hard)}"]
    try:
        import grp
        names = {grp.getgrgid(g).gr_name for g in os.getgroups()}
        parts.append("audioグループ" + ("あり" if "audio" in names else "なし(再ログイン要?)"))
    except (ImportError, KeyError, OSError):
        pass
    parts.append("busctlあり" if shutil.which("busctl") else "busctlなし")
    return ", ".join(parts)


def rt_hint() -> str:
    """RT が取れない時の対処(openSUSE 向け)。"""
    if IS_WIN:
        return "電源プランを「高パフォーマンス」にし、他のオーディオ関連ソフトを閉じてください"
    try:
        soft, _ = resource.getrlimit(resource.RLIMIT_RTPRIO)
    except (ValueError, OSError):
        soft = 0
    hints = []
    if soft == 0:
        hints.append("/etc/security/limits.d/ に「@audio - rtprio 95」「@audio - memlock unlimited」を書き、"
                     "自分を audio グループに入れて再ログイン")
    hints.append("または sudo zypper in rtkit && sudo systemctl enable --now rtkit-daemon")
    return " / ".join(hints)


def _rtkit_call(method: str, tid: int, arg: int, arg_type: str, with_pid: bool = True) -> tuple[bool, str]:
    """rtkit の D-Bus 呼び出し。busctl は別プロセスなので、素の MakeThreadRealtime(呼び出し元プロセス内のスレッド
    しか許さない)ではなく、プロセス ID を明示する *WithPID 版を使う。"""
    if with_pid:
        args = ["tt" + arg_type, str(os.getpid()), str(tid), str(arg)]
        method = method + "WithPID"
    else:
        args = ["t" + arg_type, str(tid), str(arg)]
    r = subprocess.run(
        ["busctl", "--system", "call", "org.freedesktop.RealtimeKit1", "/org/freedesktop/RealtimeKit1",
         "org.freedesktop.RealtimeKit1", method, *args],
        capture_output=True, text=True, timeout=3)
    return r.returncode == 0, f"{method}: " + (r.stderr or r.stdout).strip()[:160]


def try_realtime(prio: int) -> tuple[bool, str]:
    """呼び出しスレッドを SCHED_FIFO にする。権限が無ければ rtkit を試す。"""
    if prio <= 0:
        return False, "disabled"
    if IS_WIN:
        return _try_realtime_win()
    errs: list[str] = []
    # 1) 直接。RLIMIT_RTPRIO が prio より小さいと EPERM になるので上限に合わせて試す
    cands = [prio]
    try:
        soft, _ = resource.getrlimit(resource.RLIMIT_RTPRIO)
        if soft != resource.RLIM_INFINITY and 0 < soft < prio:
            cands = [soft]
    except (ValueError, OSError):
        pass
    for pol, name in ((os.SCHED_FIFO, "FIFO"), (getattr(os, "SCHED_RR", None), "RR")):
        if pol is None:
            continue
        for pr in cands:
            for flags in (getattr(os, "SCHED_RESET_ON_FORK", 0), 0):
                try:
                    os.sched_setscheduler(0, pol | flags, os.sched_param(pr))
                    return True, f"SCHED_{name} {pr}"
                except (PermissionError, OSError, AttributeError) as e:
                    if not errs or errs[-1] != f"{name}:{e}":
                        errs.append(f"{name}:{e}")
            break
    # 2) rtkit
    if shutil.which("busctl"):
        try:
            try:   # rtkit は RLIMIT_RTTIME が有限であることを要求する
                resource.setrlimit(resource.RLIMIT_RTTIME, (200_000, 200_000))
            except (ValueError, OSError):
                pass
            tid = threading.get_native_id()
            last = ""
            for pr in dict.fromkeys([min(prio, 20), 10, 5, 1]):
                ok, last = _rtkit_call("MakeThreadRealtime", tid, pr, "u")
                if ok:
                    return True, f"rtkit {pr}"
                if last not in errs:
                    errs.append(last)
            ok, last2 = _rtkit_call("MakeThreadHighPriority", tid, -15, "i")
            if ok:
                return False, "RT不可・高優先度(nice -15)のみ取得 (" + "; ".join(errs) + ")"
        except (OSError, subprocess.SubprocessError) as e:
            errs.append(f"rtkit例外:{e}")
    return False, "; ".join(errs) + " [" + rt_diagnosis() + "]"


def _try_realtime_win() -> tuple[bool, str]:
    """Windows: MMCSS(Pro Audio) + THREAD_PRIORITY_TIME_CRITICAL + タイマー分解能 1ms。"""
    import ctypes
    from ctypes import wintypes

    got: list[str] = []
    errs: list[str] = []
    try:
        ctypes.windll.winmm.timeBeginPeriod(1)
        got.append("timer 1ms")
    except Exception as e:  # noqa: BLE001
        errs.append(f"timeBeginPeriod:{e}")
    try:
        avrt = ctypes.windll.avrt
        avrt.AvSetMmThreadCharacteristicsW.argtypes = [wintypes.LPCWSTR, ctypes.POINTER(wintypes.DWORD)]
        avrt.AvSetMmThreadCharacteristicsW.restype = wintypes.HANDLE
        idx = wintypes.DWORD(0)
        h = avrt.AvSetMmThreadCharacteristicsW("Pro Audio", ctypes.byref(idx))
        if h:
            got.append("MMCSS Pro Audio")
            try:
                avrt.AvSetMmThreadPriority(h, 2)      # AVRT_PRIORITY_HIGH
            except Exception:  # noqa: BLE001
                pass
        else:
            errs.append(f"MMCSS 失敗({ctypes.GetLastError()})")
    except Exception as e:  # noqa: BLE001
        errs.append(f"MMCSS:{e}")
    try:
        k32 = ctypes.windll.kernel32
        k32.GetCurrentThread.restype = ctypes.c_void_p
        k32.SetThreadPriority.argtypes = [ctypes.c_void_p, ctypes.c_int]
        if k32.SetThreadPriority(k32.GetCurrentThread(), 15):      # TIME_CRITICAL
            got.append("TIME_CRITICAL")
        else:
            errs.append("SetThreadPriority 失敗")
    except Exception as e:  # noqa: BLE001
        errs.append(f"SetThreadPriority:{e}")
    ok = "TIME_CRITICAL" in got or "MMCSS Pro Audio" in got
    return ok, ", ".join(got) if ok else "; ".join(errs)


def thread_ctxt_switches(tid: int | None = None) -> tuple[int, int]:
    """(自発的, 非自発的) コンテキストスイッチ数。非自発が増えると出力スレッドが横取りされている。"""
    if IS_WIN:
        return 0, 0
    tid = tid or threading.get_native_id()
    try:
        txt = Path(f"/proc/self/task/{tid}/status").read_text()
    except OSError:
        return 0, 0
    v = nv = 0
    for line in txt.splitlines():
        if line.startswith("voluntary_ctxt_switches"):
            v = int(line.split()[1])
        elif line.startswith("nonvoluntary_ctxt_switches"):
            nv = int(line.split()[1])
    return v, nv
