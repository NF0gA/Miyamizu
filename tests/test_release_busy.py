"""busy 時の段階的な解放: suspend → (まだ busy なら) カードプロファイル off → 復元、を偽の pactl/ALSA で確認。"""
import os, sys, time
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from harness import Harness


def main():
    H = Harness("capture-paced", "S32_LE", settings=dict(busy_retry_s=6.0, release_profile_off=True))
    eng = H.eng
    from miyamizu import alsa, pwutil
    from miyamizu.settings import Settings
    fails = 0
    def check(name, ok, extra=""):
        nonlocal fails
        print(("PASS " if ok else "FAIL ") + name, extra); fails += (not ok)
    try:
        eng.test_mode = False
        eng.settings = Settings.from_dict(dict(busy_retry_s=6.0, release_profile_off=True, release_pipewire=True))
        calls = []
        state = {"profile": "output:iec958-stereo", "open": True}
        pwutil.suspend_sinks_for_card = lambda idx: (calls.append(("suspend", idx)), ["sink0"])[1]
        pwutil.resume_sinks = lambda names: calls.append(("resume_sinks", tuple(names)))
        pwutil.resume_card = lambda idx: calls.append(("resume_card", idx))
        pwutil.find_card = lambda idx: {"name": "alsa_card.usb-X8", "active_profile": state["profile"]}
        def setp(name, prof):
            calls.append(("profile", name, prof)); state["profile"] = prof
            if prof == "off": state["open"] = False
            return True
        pwutil.set_card_profile = setp
        pwutil.pcm_owners = lambda idx: ["wireplumber(1234)"] if state["open"] else []
        eng._card_index = lambda device: 2
        n = {"c": 0}
        def probe(device):
            n["c"] += 1
            if state["open"]:
                return alsa.Caps(device=device, ok=False, error="busy", busy=True)
            return alsa.Caps(device=device, ok=True, rates=[44100, 192000], formats=["S32_LE"], ch_min=2, ch_max=2)
        alsa.probe_caps = probe
        t = time.monotonic()
        caps = eng._probe_with_retry("hw:2,0")
        dt = time.monotonic() - t
        check("suspend で解放できず busy → profile off に切替えて成功", caps.ok and ("profile", "alsa_card.usb-X8", "off") in calls, f"{dt:.1f}s calls={calls[:6]}")
        check("元のプロファイルを記録", eng._profiles_off == {2: ("alsa_card.usb-X8", "output:iec958-stereo")}, str(eng._profiles_off))
        check("復元用ファイルが書かれる", eng._pending_path().exists())
        eng._cleanup_device_state()
        check("解放時に元のプロファイルへ復元", state["profile"] == "output:iec958-stereo" and not eng._profiles_off, state["profile"])
        check("復元用ファイルが消える", not eng._pending_path().exists())
        # release_profile_off=False なら off にせず失敗 + 原因プロセス名
        eng.settings = Settings.from_dict(dict(busy_retry_s=2.0, release_profile_off=False, release_pipewire=True))
        state.update(profile="output:iec958-stereo", open=True); calls.clear()
        try:
            eng._probe_with_retry("hw:2,0"); ok = False; msg = ""
        except Exception as e:  # noqa: BLE001
            ok = "wireplumber(1234)" in str(e); msg = str(e)
        check("解放不可なら使用中のプロセス名を表示", ok, msg)
        check("オフ設定では profile を触らない", not any(c[0] == "profile" for c in calls))
    finally:
        eng.test_mode = True
        H.close()
    print("ALL PASS" if not fails else f"{fails} FAIL")


if __name__ == "__main__":
    main()
