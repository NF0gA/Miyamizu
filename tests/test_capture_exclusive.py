"""アプリ音声の取り込み: 排他専用(共有では拒否)、先にアプリを移してから DAC を開く順序、無音ウォッチドッグ。"""
import os, sys, time
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from harness import Harness


def main():
    H = Harness("capture-paced", "S32_LE", settings=dict(idle_close_s=0))
    eng = H.eng
    from miyamizu import engine as E, pwutil
    from miyamizu.settings import Settings
    fails = 0
    def check(name, ok, extra=""):
        nonlocal fails
        print(("PASS " if ok else "FAIL ") + name, extra); fails += (not ok)
    def errs(m): return [e for e in H.events[m:] if e["ev"] == "error"]
    try:
        eng.test_mode = False
        E.shutil_which = lambda n: "/usr/bin/" + n
        calls = []
        pwutil.capture_begin = lambda si, rate, ch=2: (calls.append("begin"), {"module": 1, "sink_input": si, "prev_sink": "dac", "monitor": "m.monitor"})[1]
        _orig_end = pwutil.capture_end
        pwutil.capture_end = lambda st, dest=None: calls.append("end") if st else None
        # 1) 共有では拒否
        eng.settings = Settings.from_dict(dict(backend="pipewire"))
        m = H.mark(); eng.start_capture(7, "x"); time.sleep(0.3)
        check("共有出力では取り込みを開始しない", eng._cap is None and calls == [] and any("排他出力専用" in e["msg"] for e in errs(m)))
        # 2) 排他: 先にアプリを移し、DAC を開く(prepare)のは後。失敗時は元に戻す
        eng.settings = Settings.from_dict(dict(backend="alsa_hw", alsa_device="hw:2,0"))
        def prep(info): calls.append("prepare"); raise E.EngineError("開けない")
        eng._prepare_session = prep
        m = H.mark(); eng.start_capture(7, "x"); time.sleep(0.3)
        check("順序: アプリを移す → デバイスを開く → 失敗なら戻す", calls == ["begin", "prepare", "end"], str(calls))
        check("失敗は通知され取り込み状態は残らない", eng._cap is None and any("開けない" in e["msg"] for e in errs(m)))
        # 3) 無音ウォッチドッグ
        class S:  # 何も届いていない取り込み
            class shm: write_pos = 0
        eng._cap = {"sid": 1, "src": S, "pw": {"sink_input": 7, "module": 1, "prev_sink": "dac", "monitor": "m"}, "t0": time.monotonic() - 10, "checked": False}
        pwutil.list_app_streams = lambda exclude_pids=None: [{"index": 7, "sink_name": "alsa_output.x"}]
        m = H.mark(); eng._capture_watchdog(); time.sleep(0.3)
        check("音が届かない時に警告(出力先つき)", any("届いていません" in e["msg"] and "alsa_output.x" in e["msg"] for e in errs(m)))
        m = H.mark(); eng._capture_watchdog(); time.sleep(0.2)
        check("警告は1回だけ", not errs(m))
        eng._cap = None
        # 4) 選んだアプリ以外が取り込み用シンクに入ったら元の出力へ戻す(ブラウザを先に選んだ後の再生など)
        import json, subprocess
        moves = []
        pwutil.shutil.which = lambda n: "/usr/bin/" + n
        pwutil.list_sinks = lambda: [{"index": 9, "name": pwutil.CAPTURE_SINK}, {"index": 1, "name": "dac"}]
        items = [{"index": 30, "sink": 9, "properties": {"application.name": "Game"}},
                 {"index": 31, "sink": 9, "properties": {"application.name": "Firefox"}},
                 {"index": 32, "sink": 1, "properties": {"application.name": "Other"}}]
        class R:
            def __init__(s, out="", rc=0): s.stdout, s.returncode = out, rc
        def fake_run(cmd, timeout=3.0):
            if cmd[:4] == ["pactl", "--format=json", "list", "sink-inputs"]: return R(json.dumps(items))
            if cmd[:2] == ["pactl", "move-sink-input"]: moves.append(cmd[2:]); return R()
            return R()
        pwutil._run = fake_run
        got = pwutil.capture_evict_others({"sink_input": 30, "prev_sink": "dac"})
        check("選択外のストリームだけ元の出力へ戻す", got == ["Firefox"] and moves == [["31", "dac"]], f"{got} {moves}")
        moves.clear(); items[1]["sink"] = 1
        check("紛れ込みが無ければ何もしない", pwutil.capture_evict_others({"sink_input": 30, "prev_sink": "dac"}) == [] and not moves)
        # 5) 同じアプリの作り直されたストリーム(シーク等)は追従、他アプリは戻す。後始末で全て dac へ
        moves.clear()
        items[:] = [{"index": 40, "sink": 1, "properties": {"application.name": "Firefox", "application.process.binary": "firefox"}},
                    {"index": 41, "sink": 9, "properties": {"application.name": "Game"}}]
        st = {"module": 1, "sink_input": 30, "prev_sink": "dac", "key": ("Firefox", "firefox"), "touched": {("Firefox", "firefox")}}
        got = pwutil.capture_evict_others(st)
        check("同じアプリの新ストリームは取り込み側へ追従", ["40", pwutil.CAPTURE_SINK] in moves and st["sink_input"] == 40, f"{moves} {st}")
        check("別アプリは戻し、後始末の対象に記録", got == ["Game"] and ("Game", "") in st["touched"], f"{got}")
        moves.clear()
        pwutil.capture_end = _orig_end
        items[0]["sink"] = 9
        pwutil.capture_end(st)
        check("終了時に触ったアプリを全て元の出力へ", ["40", "dac"] in moves and ["41", "dac"] in moves, f"{moves}")
        moves.clear()
        items[0]["sink"] = 1
        n = pwutil.move_app_streams({("Firefox", "firefox")}, "main")
        check("メイン機器へ戻す", n == 1 and moves == [["40", "main"]], f"{n} {moves}")
    finally:
        eng.test_mode = True
        H.close()
    print("ALL PASS" if not fails else f"{fails} FAIL")


if __name__ == "__main__":
    main()
