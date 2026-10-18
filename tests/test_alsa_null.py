import os, sys, time
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from harness import *
D = "/tmp/claude-0/-home-claude/634049f8-76f1-5b8b-ba6e-ce947dd8eb75/scratchpad/media/"
def main():
    H = Harness(None, "S32_LE", settings=dict(backend="custom", custom_device="null", dedicated_cpu=1, rt_priority=10, idle_close_s=0, ram_playback=True))
    try:
        m = H.mark()
        H.cmd("load", track={"id": 1, "path": D + "n1.flac"})
        H.wait("finished", timeout=20, since=m)
        H.wait("metrics", timeout=5)
        met = [e for e in H.events if e["ev"] == "metrics"][-1]
        print("metrics out:", {k: met["out"][k] for k in ("rt", "affinity", "underruns", "xruns", "period_frames", "buffer_frames")})
        print("ded/others:", met["ded"], met["others"])
        err = [e for e in H.events if e["ev"] == "error"]
        print("errors:", err)
        tc = [e for e in H.events if e["ev"] == "track_changed"]
        print("signal:", tc[0]["signal"] if tc else None)
    finally:
        H.close()
if __name__ == "__main__":
    main()
