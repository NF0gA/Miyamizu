"""メイン機器の割り当て: 共有 → 排他(OSから外す) → 共有(戻して既定出力に) を偽の pactl で確認。"""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from harness import Harness


def main():
    H = Harness("capture-paced", "S32_LE")
    eng = H.eng
    from miyamizu import pwutil
    from miyamizu.settings import Settings
    fails = 0
    def check(name, ok, extra=""):
        nonlocal fails
        print(("PASS " if ok else "FAIL ") + name, extra); fails += (not ok)
    try:
        eng.test_mode = False
        st = {"profile": "output:iec958-stereo", "default": "sink_other"}
        calls = []
        pwutil.suspend_sinks_for_card = lambda idx: []
        pwutil.resume_sinks = lambda n: None
        pwutil.resume_card = lambda idx: None
        pwutil.find_card = lambda idx: {"name": "card_x8", "active_profile": st["profile"]}
        def setp(n, p): st["profile"] = p; calls.append(("profile", p)); return True
        pwutil.set_card_profile = setp
        pwutil.wait_sink_for_card = lambda idx, timeout=4.0: "sink_x8" if st["profile"] != "off" else None
        def setd(n): st["default"] = n; return True
        pwutil.set_default_sink = setd
        eng._card_index = lambda d: 2
        eng._main_card = lambda: 2
        def cfg(**kw):
            eng.settings = Settings.from_dict(dict(backend=kw.pop("backend"), alsa_device="hw:2,0", **kw))
            eng.apply_assignment()
        cfg(backend="pipewire")
        check("起動時の共有では既定出力に触らない", st["default"] == "sink_other")
        cfg(backend="alsa_hw")
        check("排他: プロファイル off で専有", st["profile"] == "off" and 2 in eng._claimed)
        eng._cleanup_device_state(keep_claim=True)
        check("アイドルで閉じても専有を保つ", st["profile"] == "off")
        eng._cleanup_device_state()
        check("手動解放などでは復元", st["profile"] == "output:iec958-stereo" and not eng._claimed)
        cfg(backend="alsa_hw")
        eng.release_device("出力設定の変更")        # configure が出力変更時に行う解放
        cfg(backend="pipewire")
        check("共有へ戻すとメイン機器が既定出力に", st["default"] == "sink_x8", str(st))
        check("プロファイルが元に戻る", st["profile"] == "output:iec958-stereo")
        st["default"] = "sink_other"
        cfg(backend="pipewire", default_on_shared=False)
        check("既定出力変更をオフにできる", st["default"] == "sink_other")
        st["profile"] = "output:iec958-stereo"
        cfg(backend="alsa_hw", claim_on_switch=False)
        check("専有オフなら触らない", st["profile"] == "output:iec958-stereo")
    finally:
        eng.test_mode = True
        H.close()
    print("ALL PASS" if not fails else f"{fails} FAIL")


if __name__ == "__main__":
    main()
