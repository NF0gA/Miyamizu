"""エンジンを同一プロセスのスレッドで起動し、ソケット経由で操作するテスト用ハーネス。"""
import json, os, socket, sys, tempfile, threading, time
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import numpy as np
from miyamizu.engine import Engine
from miyamizu import convert


class Harness:
    def __init__(self, backend="capture-paced", fmt="S32_LE", settings=None):
        self.dir = tempfile.mkdtemp(prefix="hifitest-")
        self.sock_path = os.path.join(self.dir, "e.sock")
        self.eng = Engine(self.sock_path, backend, fmt, enable_rt=False)
        self.thread = threading.Thread(target=self.eng.serve, daemon=True)
        self.thread.start()
        time.sleep(1.5)   # 準備プロセス(spawn)の起動待ち
        t = time.time()
        while not os.path.exists(self.sock_path) and time.time() - t < 10:
            time.sleep(0.02)
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.connect(self.sock_path)
        self.events = []
        self.lock = threading.Lock()
        self._rd = threading.Thread(target=self._read, daemon=True)
        self._rd.start()
        if settings is not None:
            self.configure(**settings)

    def _read(self):
        buf = b""
        while True:
            try:
                d = self.sock.recv(65536)
            except OSError:
                return
            if not d:
                return
            buf += d
            while b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
                with self.lock:
                    self.events.append(json.loads(line))

    def cmd(self, cmd, **kw):
        self.sock.sendall((json.dumps({"cmd": cmd, **kw}) + "\n").encode())

    def configure(self, **kw):
        from miyamizu.settings import Settings
        s = Settings.from_dict(kw)
        self.cmd("configure", settings=s.to_dict())
        time.sleep(0.1)

    def wait(self, ev, timeout=20, pred=None, since=0):
        t = time.time()
        i = since
        while time.time() - t < timeout:
            with self.lock:
                evs = self.events[i:]
                n = len(self.events)
            for e in evs:
                if e["ev"] == ev and (pred is None or pred(e)):
                    return e
            i = n
            time.sleep(0.01)
        raise TimeoutError(f"event {ev} not seen; last: {self.events[-5:]}")

    def mark(self):
        with self.lock:
            return len(self.events)

    def count(self, ev, since=0):
        with self.lock:
            return sum(1 for e in self.events[since:] if e["ev"] == ev)

    def captured(self):
        be = self.eng._test_backend_obj
        return b"".join(be.chunks)

    def close(self):
        try:
            self.cmd("quit")
        except OSError:
            pass
        self.thread.join(timeout=10)


def make_noise_flac(path, seconds, rate=44100, bits=16, ch=2, seed=0):
    import soundfile as sf
    rng = np.random.default_rng(seed)
    n = int(seconds * rate)
    amp = (1 << (bits - 1)) - 1
    x = rng.integers(-amp // 2, amp // 2, size=(n, ch)).astype(np.int32)
    x[x == 0] = 1
    sub = {16: "PCM_16", 24: "PCM_24"}[bits]
    sf.write(path, x if bits == 24 else x.astype(np.int16), rate, subtype=sub, format="FLAC")
    return x << (32 - bits)    # left-justified int32 の期待値


def left_i32_from_stream(raw, fmt, ch):
    return convert.unpack(np.frombuffer(raw, dtype=np.uint8), fmt, ch)


def find_start(x):
    nz = np.nonzero(np.any(x != 0, axis=1))[0]
    return int(nz[0]) if len(nz) else None
