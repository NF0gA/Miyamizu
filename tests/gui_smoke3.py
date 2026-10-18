"""GUI スモークテスト 3: 出力プロファイル切替(再生位置を保って再開)・事前変換の表示・診断レポート。"""
import os, sys, tempfile, time, traceback
tmp = tempfile.mkdtemp()
os.environ.update(QT_QPA_PLATFORM="offscreen", XDG_CONFIG_HOME=tmp+"/c", XDG_DATA_HOME=tmp+"/d",
                  XDG_RUNTIME_DIR=tmp, MIYAMIZU_ENGINE_ARGS="--test-backend null --no-rt")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.excepthook = lambda *a: traceback.print_exception(*a)
from PyQt6.QtWidgets import QApplication
app = QApplication(sys.argv)
from miyamizu.__main__ import apply_dark
apply_dark(app)
from miyamizu.gui.main_window import MainWindow
from miyamizu.settings import load_settings, save_settings
s = load_settings(); s.library_dir = sys.argv[1]; s.ram_playback = True; s.prerender_ahead = 2; save_settings(s)
w = MainWindow(); w.resize(1400, 800); w.show()
def pump(sec):
    end = time.monotonic()+sec
    while time.monotonic() < end:
        app.processEvents(); time.sleep(0.01)
fails = 0
def ok(c, m):
    global fails
    print(("PASS " if c else "FAIL ") + m); fails += (not c)
pump(4)
print("profiles:", [w.cmb_profile.itemText(i) for i in range(w.cmb_profile.count())], "| sel:", w.cmb_profile.currentText())
ok(w.cmb_profile.count() >= 2 and w.cmb_profile.currentText().startswith("出力: 共有"), "既定プロファイル(共有/排他)が出る")
w.play_from_view(0); pump(3.5)
col = [w.track_model.data(w.track_model.index(r, 1)) for r in range(3)]
print("RAM col:", col, "maps:", w._ram_maps[0])
ok(all(col), "先行変換により 3 曲すべてが RAM 列に出る(再生中+次曲+先行)")
roles = sorted(v["role"] for v in w._ram_maps[0].values())
ok(roles == ["ahead", "cur", "next"], f"role: {roles}")
pos_before = w._pos
w.apply_profile("排他 (ALSA hw)"); pump(2.5)
ok(w.settings.backend == "alsa_hw", "プロファイル適用で backend が変わる")
ok(w.engine_state == "playing" and w._pos >= pos_before - 0.5, f"切替後に同じ位置付近から再開 (before={pos_before:.1f}s now={w._pos:.1f}s state={w.engine_state})")
ok(w.cmb_profile.currentText().startswith("出力: 排他"), "コンボの表示が追従: " + w.cmb_profile.currentText())
w.apply_profile("共有 (PipeWire)"); pump(2.0)
ok(w.settings.backend == "pipewire" and w.engine_state == "playing", "共有へ戻せる")
rep = w.build_diagnostics()
print(rep[:400].replace("\n", " | "))
ok("settings:" in rep and "signal path" in rep and "metrics" in rep, "診断レポートが組み立てられる")
w.grab().save(sys.argv[2])
w.close(); pump(1)
print("ALL PASS" if not fails else f"{fails} FAIL")
