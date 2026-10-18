"""シークバーのクリック位置 → 送られる seek 位置を確認(右端まで)。"""
import os, sys, tempfile, time, traceback
tmp = tempfile.mkdtemp()
os.environ.update(QT_QPA_PLATFORM="offscreen", XDG_CONFIG_HOME=tmp+"/c", XDG_DATA_HOME=tmp+"/d",
                  XDG_RUNTIME_DIR=tmp, MIYAMIZU_ENGINE_ARGS="--test-backend null --no-rt")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.excepthook = lambda *a: traceback.print_exception(*a)
from PyQt6.QtWidgets import QApplication, QStyle, QStyleOptionSlider
from PyQt6.QtCore import Qt, QPoint
from PyQt6.QtTest import QTest
app = QApplication(sys.argv)
from miyamizu.__main__ import apply_dark
apply_dark(app)
from miyamizu.gui.main_window import MainWindow
from miyamizu.settings import load_settings, save_settings
s = load_settings(); s.library_dir = sys.argv[1]; save_settings(s)
w = MainWindow(); w.resize(1300, 700); w.show()
def pump(sec):
    end = time.monotonic()+sec
    while time.monotonic() < end:
        app.processEvents(); time.sleep(0.01)
pump(4); w.play_from_view(0); pump(1.5)
sent = []
orig = w.client.send
w.client.send = lambda cmd, **kw: (sent.append((cmd, kw)), orig(cmd, **kw))[1]
sl = w.seek
opt = QStyleOptionSlider(); sl.initStyleOption(opt)
groove = sl.style().subControlRect(QStyle.ComplexControl.CC_Slider, opt, QStyle.SubControl.SC_SliderGroove, sl)
print("slider width", sl.width(), "groove", groove.left(), groove.right(), "dur", w.cur_dur)
bad = 0
for frac in (0.1, 0.5, 0.9, 0.95, 0.98, 0.995):
    sent.clear()
    x = int(groove.left() + frac * (groove.width() - 1)); y = sl.height() // 2
    QTest.mouseClick(sl, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, QPoint(x, y)); pump(0.2)
    seeks = [kw["pos"] for c, kw in sent if c == "seek"]
    exp = frac * w.cur_dur
    ok = bool(seeks) and abs(seeks[-1] - exp) < 0.25 * w.cur_dur / 6 + 0.1
    bad += not ok
    print(f"click {frac:.3f}: seek={seeks} expected~{exp:.2f}", "OK" if ok else "FAIL", "seeking" if w.seeking else "")
    pump(0.6)
    if w.engine_state != "playing":
        w.play_from_view(0); pump(1.2)
print("ALL PASS" if not bad and not w.seeking else "FAIL")
w.close(); pump(1)
