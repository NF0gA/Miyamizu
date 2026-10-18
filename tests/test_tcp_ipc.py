"""Windows 用の TCP 接続(127.0.0.1 + トークン認証)を Linux 上で検証する(MIYAMIZU_TCP=1)。"""
import json, os, socket, sys, tempfile, threading, time
os.environ["MIYAMIZU_TCP"] = "1"
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from miyamizu.engine import Engine

def recv_lines(s, n_sec=3.0):
    s.settimeout(0.3); buf = b""; end = time.time() + n_sec; out = []
    while time.time() < end:
        try:
            x = s.recv(65536)
        except socket.timeout:
            continue
        except OSError:
            return out, True
        if not x:
            return out, True
        buf += x
        while b"\n" in buf:
            l, buf = buf.split(b"\n", 1)
            out.append(json.loads(l))
        if any(e.get("ev") == "hello" for e in out):
            return out, False
    return out, False



def main():
    d = tempfile.mkdtemp(prefix="tcptest-")
    port_file = os.path.join(d, "e.port")
    eng = Engine(port_file, "null", "S32_LE", enable_rt=False)
    threading.Thread(target=eng.serve, daemon=True).start()
    t = time.time()
    while not os.path.exists(port_file) and time.time() - t < 10:
        time.sleep(0.05)
    info = json.load(open(port_file))


    # 1) 認証なし → 切断され何も返らない
    a = socket.create_connection(("127.0.0.1", info["port"]))
    a.sendall(b'{"cmd":"hello"}\n')
    evs, closed = recv_lines(a, 2.0)
    assert closed and not any(e["ev"] == "hello" for e in evs), (evs, closed)
    # 2) 誤トークン → 切断
    b = socket.create_connection(("127.0.0.1", info["port"]))
    b.sendall(b'{"cmd":"auth","token":"bad"}\n{"cmd":"hello"}\n')
    evs, closed = recv_lines(b, 2.0)
    assert closed and not evs, (evs, closed)
    # 3) 正しいトークン → hello が返る
    c = socket.create_connection(("127.0.0.1", info["port"]))
    c.sendall((json.dumps({"cmd": "auth", "token": info["token"]}) + "\n" + '{"cmd":"hello"}\n').encode())
    evs, closed = recv_lines(c, 5.0)
    assert any(e["ev"] == "hello" for e in evs), evs
    eng.quit = True
    time.sleep(0.5)
    assert not os.path.exists(port_file)
    print("PASS tcp ipc: 未認証/誤トークンは切断、正しいトークンで接続")


if __name__ == "__main__":
    main()
