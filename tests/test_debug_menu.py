"""デバッグ機能: 既定では非表示、設定 → その他 で有効化するとメニューに出る。ミキサー機能は無い。"""
import os, sys, tempfile, time, traceback
tmp = tempfile.mkdtemp()
os.environ.update(QT_QPA_PLATFORM="offscreen", XDG_CONFIG_HOME=tmp+"/c", XDG_DATA_HOME=tmp+"/d", XDG_RUNTIME_DIR=tmp, MIYAMIZU_ENGINE_ARGS="--test-backend null --no-rt")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.excepthook = lambda *a: traceback.print_exception(*a)


def main():
    from PyQt6.QtWidgets import QApplication
    app = QApplication(sys.argv)
    from miyamizu.gui import main_window as mw
    from miyamizu.gui.settings_dialog import SettingsDialog
    fails = 0
    def check(n, ok, x=""):
        nonlocal fails
        print(("PASS " if ok else "FAIL ") + n, x); fails += (not ok)
    w = mw.MainWindow(); w.show()
    end = time.monotonic() + 2
    while time.monotonic() < end: app.processEvents(); time.sleep(0.01)
    check("既定ではデバッグメニューは非表示", not w.menu_debug.menuAction().isVisible())
    d = SettingsDialog(w.settings, w.engine_hello)
    check("設定に「その他」タブがある", any(d.findChildren(type(d.debug)) ) and d.debug.isChecked() is False)
    d.debug.setChecked(True)
    check("設定の値が反映される", d.result_settings().debug_enabled is True)
    w.settings.debug_enabled = True
    w.menu_debug.menuAction().setVisible(True)
    check("有効にするとメニューに ABX がある", w.menu_debug.menuAction().isVisible() and any("ABX" in a.text() for a in w.menu_debug.actions()))
    check("ミキサー機能は無い", not hasattr(mw, "MixerDialog") and not hasattr(w, "open_mixer_dialog"))
    w.close()
    print("ALL PASS" if not fails else f"{fails} FAIL")
    sys.exit(1 if fails else 0)


if __name__ == "__main__":
    main()
