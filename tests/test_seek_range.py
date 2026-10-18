"""全域シーク: 曲の終端近くへシークしても位置が進み、最後まで再生されること。"""
import os, sys, time
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from harness import Harness, make_noise_flac
def main():
    D = "/tmp/hifi_seek/"; os.makedirs(D, exist_ok=True)
    make_noise_flac(D + "s.flac", 12.0)
    fails = 0
    for ram in (True, False):
        H = Harness("capture-paced", "S32_LE", settings=dict(ram_playback=ram, idle_close_s=0))
        try:
            H.cmd("load", track={"id": 1, "path": D + "s.flac"}); time.sleep(1.0)
            for tgt in (3.0, 6.0, 9.0, 10.5, 11.0, 11.5, 11.8, 11.95, 12.0):
                H.cmd("seek", pos=tgt)
                time.sleep(0.5)
                st = [e for e in H.events if e["ev"] == "status"][-1]
                fin = any(e["ev"] == "finished" for e in H.events[-40:])
                ok = (st["state"] == "playing" and st["pos"] >= tgt - 0.1) or fin or st["state"] == "stopped"
                print(f"ram={ram} seek {tgt}: state={st['state']} pos={st['pos']} finished={fin}", "OK" if ok else "FAIL")
                fails += not ok
                if fin or st["state"] == "stopped":
                    H.cmd("load", track={"id": 1, "path": D + "s.flac"}); time.sleep(0.8)
        finally:
            H.close()
    print("ALL PASS" if not fails else f"{fails} FAIL")


if __name__ == '__main__':
    main()
