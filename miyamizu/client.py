"""GUI 側のエンジン接続(QLocalSocket)。エンジンが居なければ起動する。"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

from PyQt6.QtCore import QObject, QTimer, pyqtSignal
from PyQt6.QtNetwork import QAbstractSocket, QLocalSocket, QTcpSocket

from .common import data_dir, default_socket_path, use_tcp

PROJECT_ROOT = str(Path(__file__).resolve().parent.parent)


def _reset_affinity():
    """エンジンは GUI の CPU 制限を引き継がない(専用コア等の割り当てはエンジン自身が行う)。"""
    try:
        n = os.cpu_count() or 1
        os.sched_setaffinity(0, set(range(n)))
    except (AttributeError, OSError):
        pass


class EngineClient(QObject):
    ev_received = pyqtSignal(dict)      # NOTE: QObject.event を隠さないよう別名
    connected = pyqtSignal()
    disconnected = pyqtSignal()

    def __init__(self, sock_path: str | None = None, parent=None):
        super().__init__(parent)
        self.sock_path = sock_path or default_socket_path()
        self.tcp = use_tcp()
        self.token = ""
        self.sock = QTcpSocket(self) if self.tcp else QLocalSocket(self)
        self.sock.readyRead.connect(self._on_ready)
        self.sock.connected.connect(lambda: self.connected.emit())
        self.sock.disconnected.connect(self._on_sock_disc)
        self._buf = b""
        self.proc: subprocess.Popen | None = None
        self.log_path = data_dir() / "engine.log"

    def _on_sock_disc(self):
        try:
            self.disconnected.emit()
        except RuntimeError:     # 終了処理中に親が破棄済み
            pass

    # ---- 起動/接続 ----
    def start(self, timeout_s: float = 15.0) -> bool:
        """既存エンジンに接続。無ければ起動して接続。成功なら True。"""
        if self._try_connect(0.3):
            return True
        self._spawn()
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            if self._try_connect(0.3):
                return True
            if self.proc is not None and self.proc.poll() is not None:
                return False
            time.sleep(0.15)
        return False

    def _try_connect(self, wait_s: float) -> bool:
        if not os.path.exists(self.sock_path):
            return False
        self.sock.abort()
        if self.tcp:
            try:
                info = json.loads(Path(self.sock_path).read_text())
                port, self.token = int(info["port"]), str(info["token"])
            except (OSError, ValueError, KeyError):
                return False
            self.sock.connectToHost("127.0.0.1", port)
            ok = self.sock.waitForConnected(int(wait_s * 1000))
            if ok:      # 最初の1行で認証(他のローカルプロセスからの接続を弾く)
                self.sock.write((json.dumps({"cmd": "auth", "token": self.token}) + "\n").encode())
            return ok
        self.sock.connectToServer(self.sock_path)
        return self.sock.waitForConnected(int(wait_s * 1000))

    def _is_connected(self) -> bool:
        if self.tcp:
            return self.sock.state() == QAbstractSocket.SocketState.ConnectedState
        return self.sock.state() == QLocalSocket.LocalSocketState.ConnectedState

    def _spawn(self):
        logf = open(self.log_path, "ab")
        env = dict(os.environ)
        env["PYTHONPATH"] = PROJECT_ROOT + os.pathsep + env.get("PYTHONPATH", "")
        extra = (os.environ.get("MIYAMIZU_ENGINE_ARGS") or os.environ.get("HIFIPLAYER_ENGINE_ARGS", "")).split()   # テスト用 (例: --test-backend null)
        if getattr(sys, "frozen", False):        # PyInstaller 版: 同じ実行ファイルを --engine で再起動する
            argv = [sys.executable, "--engine", "--socket", self.sock_path, *extra]
            cwd = str(Path(sys.executable).resolve().parent)
        else:
            argv = [sys.executable, "-m", "miyamizu.engine", "--socket", self.sock_path, *extra]
            cwd = PROJECT_ROOT
        if sys.platform == "win32":
            self.proc = subprocess.Popen(argv, cwd=cwd, env=env, stdout=logf, stderr=logf, stdin=subprocess.DEVNULL,
                                         creationflags=0x08000000)        # CREATE_NO_WINDOW
        else:
            self.proc = subprocess.Popen(
                argv,
                cwd=cwd, env=env, stdout=logf, stderr=logf, start_new_session=True,
                preexec_fn=_reset_affinity)

    def shutdown_engine(self):
        """エンジンを終了させる(GUI 終了時)。"""
        try:
            self.send("quit")
            self.sock.flush()
            self.sock.waitForBytesWritten(500)
        except Exception:  # noqa: BLE001
            pass
        if self.proc is not None:
            try:
                self.proc.wait(timeout=4)
            except subprocess.TimeoutExpired:
                self.proc.terminate()

    # ---- 送受信 ----
    def send(self, cmd: str, **kw):
        if not self._is_connected():
            return
        self.sock.write((json.dumps({"cmd": cmd, **kw}, ensure_ascii=False) + "\n").encode())

    def _on_ready(self):
        self._buf += bytes(self.sock.readAll())
        while b"\n" in self._buf:
            line, self._buf = self._buf.split(b"\n", 1)
            if not line.strip():
                continue
            try:
                self.ev_received.emit(json.loads(line))
            except ValueError:
                pass
