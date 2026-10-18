"""GUI スモークテスト 2: CUE フォルダ化・ソート・キュー・RAM 表示・ALAC。"""
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
s = load_settings(); s.library_dir = sys.argv[1]; s.ram_playback = True; save_settings(s)
w = MainWindow(); w.resize(1400, 800); w.show()
def pump(sec):
    end = time.monotonic()+sec
    while time.monotonic() < end:
        app.processEvents(); time.sleep(0.01)
def ok(c, m): print(("PASS " if c else "FAIL ") + m)
pump(5)
root = w.tree.topLevelItem(0)
def names(it): return [it.child(i).text(0) for i in range(it.childCount())]
print("root children:", names(root))
mixed = next(root.child(i) for i in range(root.childCount()) if root.child(i).text(0) == "mixed")
mixed.setExpanded(True); pump(0.3)
print("mixed children:", names(mixed))
ok(sum(1 for n in names(mixed) if n.startswith("💿")) == 2, "CUE 2 枚がフォルダ(ノード)として並ぶ")
# CUE ノードを選択
grp = next(mixed.child(i) for i in range(mixed.childCount()) if "AlbumA" in mixed.child(i).text(0))
w.tree.setCurrentItem(grp); pump(0.3)
ok([t["title"] for t in w.view_tracks] == [f"AlbumA song {i}" for i in (1, 2, 3)], f"CUE ノード選択でそのアルバムのみ: {[t['title'] for t in w.view_tracks]}")
# 親フォルダ選択
w.tree.setCurrentItem(mixed); pump(0.3)
print("mixed view:", [t["title"] for t in w.view_tracks])
ok(len(w.view_tracks) == 7, "フォルダ表示は 7 曲(A3+B3+single)")
# ソート
w.on_header_clicked(3); ti = [t["title"] for t in w.view_tracks]
ok(ti == sorted(ti, key=str.lower), "タイトル昇順")
w.on_header_clicked(3); ti2 = [t["title"] for t in w.view_tracks]
ok(ti2 == ti[::-1], "タイトル降順")
w.on_header_clicked(3)
ok(w.sort_col is None and w.view_tracks == w.view_base, "3 回目で元の順")
# 再生 → キュー / RAM
w.tree.setCurrentItem(grp); pump(0.3)
w.play_from_view(0); pump(2.5)
q = w.queue_model.tracks
print("queue:", [t["title"] for t in q], "|", w.lbl_queue.text())
ok([t["title"] for t in q] == ["AlbumA song 1", "AlbumA song 2", "AlbumA song 3"], "キュー = 再生中 + 残り")
print("ram maps:", getattr(w, "_ram_maps", None))
col = [w.track_model.data(w.track_model.index(r, 1)) for r in range(3)]
print("RAM col:", col)
ok(any(c for c in col), "RAM 列に表示が出る")
w.on_header_clicked(3); w.on_header_clicked(3)       # 降順に: 再生順も追従
pump(0.3)
print("queue after sort desc:", [t["title"] for t in w.queue_model.tracks])
w.reset_sort()
w.grab().save(sys.argv[2])
# ALAC
w.tree.setCurrentItem(root); pump(0.3)
alac = [t for t in w.lib.all_tracks() if t["path"].endswith("alac1.m4a")]
print("alac:", alac and (alac[0]["codec"], alac[0]["rate"], alac[0]["bits"]))
ok(alac[0]["codec"]=="ALAC","ALAC 表記")
ok(bool(alac) and alac[0]["bits"] in (16, 24), "ALAC が登録され bit 深度が出る")
w.play_from_view(next(i for i, t in enumerate(w.view_tracks) if t["path"].endswith("alac1.m4a"))); pump(2)
print("signal:", w.lbl_signal.text()[:200].replace("<br>", " | "))
ok(w.engine_state == "playing", "ALAC 再生中")
w.close(); pump(1)
