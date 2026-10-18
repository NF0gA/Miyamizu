"""GUI スモークテスト(offscreen + null バックエンド)。"""
import os, sys, tempfile, time
tmp = tempfile.mkdtemp()
os.environ.update(QT_QPA_PLATFORM="offscreen", XDG_CONFIG_HOME=tmp+"/c", XDG_DATA_HOME=tmp+"/d",
                  XDG_RUNTIME_DIR=tmp, MIYAMIZU_ENGINE_ARGS="--test-backend null --no-rt")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from PyQt6.QtWidgets import QApplication
from PyQt6.QtCore import QTimer
import traceback
sys.excepthook = lambda *a: traceback.print_exception(*a)
app = QApplication(sys.argv)
from miyamizu.__main__ import apply_dark
apply_dark(app)
from miyamizu.gui.main_window import MainWindow
from miyamizu.settings import load_settings, save_settings
s = load_settings(); s.library_dir = sys.argv[1]; save_settings(s)
w = MainWindow(); w.resize(1280, 760); w.show()
def pump(sec):
    end = time.monotonic()+sec
    while time.monotonic() < end:
        app.processEvents(); time.sleep(0.01)
pump(4)
print("tracks in view:", len(w.track_model.tracks) if hasattr(w,'track_model') else '?')
w.grab().save(sys.argv[2])
w.play_from_view(0)
log=[]
t0=time.monotonic()
while time.monotonic()-t0 < 20:
    pump(0.5)
    log.append((round(time.monotonic()-t0,1), w.engine_state, w.lbl_title.text(), w.lbl_pos.text()))
for l in log[::2]: print(l)
w.grab().save(sys.argv[2].replace('1.png','2.png'))
print("sigpath:", [k for k in dir(w) if 'sig' in k.lower() or 'path' in k.lower()][:10])
w.close(); pump(1)
