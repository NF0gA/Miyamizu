"""メインウィンドウ。ドック(ディレクトリ/プレイリスト/再生バー/メトリクス)は自由に配置できる。"""
from __future__ import annotations

import base64
import html
import json
import os
from dataclasses import asdict
from pathlib import Path

from PyQt6.QtCore import QByteArray, QDir, QModelIndex, QPoint, QSettings, QSize, Qt, QThread, QTimer, pyqtSignal
from PyQt6.QtGui import QAction, QActionGroup, QDesktopServices, QIcon, QImage, QKeySequence, QPixmap
from PyQt6.QtCore import QUrl
from PyQt6.QtWidgets import (QAbstractItemView, QComboBox, QDialog, QProxyStyle, QApplication, QDockWidget, QFileDialog, QHBoxLayout, QHeaderView,
                             QInputDialog, QLabel, QListWidget, QListWidgetItem, QMainWindow, QMenu, QMessageBox,
                             QPushButton, QSlider, QSplitter, QStyle, QTableView, QToolButton, QTreeWidget,
                             QTreeWidgetItem, QVBoxLayout, QWidget)

from .. import __version__, sysinfo
from ..client import EngineClient
from ..common import IS_WIN, active_exts, fmt_time, rate_label
from ..library import Library, extract_cover, natural_key
from ..playorder import PlayOrder
from ..settings import PROFILE_KEYS, Settings, load_settings, save_settings
from .settings_dialog import SettingsDialog
from .widgets import (ArtBackground, COL_ALBUM, COL_ARTIST, COL_DUR, COL_FILE, COL_FMT, COL_NO, COL_RAM, COL_TITLE,
                      FreqWidget, HistogramWidget, MIME_TRACKS, TrackModel)

REPEAT_MODES = [("off", "リピート: オフ"), ("one", "リピート: 1曲"), ("folder", "リピート: 表示中の一覧"), ("all", "リピート: 全曲")]
LAYOUT_VER = "2"
SHUFFLE_MODES = [("off", "シャッフル: オフ"), ("folder", "シャッフル: 表示中の一覧"), ("all", "シャッフル: 全曲")]


# ------------------------------------------------------------------ バックグラウンド処理
class ScanWorker(QThread):
    progress = pyqtSignal(str, int)
    done = pyqtSignal(int)

    def __init__(self, root: str, parent=None, exts=None, reset=False):
        super().__init__(parent)
        self.root = root
        self.exts = exts
        self.reset = reset
        self._cancel = False

    def cancel(self):
        self._cancel = True

    def run(self):
        lib = Library()
        n = 0
        try:
            if self.reset:
                lib.reset_dir_cache()
            n = lib.scan(self.root, progress=lambda d, c: self.progress.emit(d, c), cancel=lambda: self._cancel, exts=self.exts)
        except Exception:  # noqa: BLE001
            pass
        finally:
            lib.close()
        self.done.emit(n)


class CoverWorker(QThread):
    loaded = pyqtSignal(str, QImage)

    def __init__(self, path: str, parent=None):
        super().__init__(parent)
        self.path = path

    def run(self):
        data = extract_cover(self.path)
        img = QImage()
        if data:
            img.loadFromData(data)      # パスではなくバイト列から読む(Qt の I/O で GUI が固まらないように)
        self.loaded.emit(self.path, img)


class _JumpStyle(QProxyStyle):
    """左クリックした位置へハンドルを直接動かす(ドラッグも通常どおり使える)。"""

    def styleHint(self, hint, option=None, widget=None, returnData=None):
        if hint == QStyle.StyleHint.SH_Slider_AbsoluteSetButtons:
            return Qt.MouseButton.LeftButton.value
        return super().styleHint(hint, option, widget, returnData)


class JumpSlider(QSlider):
    """クリックした位置へ直接ジャンプするスライダー。
    以前は mousePressEvent を自前で書いていたが、溝の余白を無視して位置がずれ、
    ハンドル以外のクリックで sliderReleased が出ずシークが無反応になる区間があった。"""

    def __init__(self, orientation, parent=None):
        super().__init__(orientation, parent)
        # QProxyStyle は渡された基底スタイルの所有権を取る。アプリ共通のスタイルを渡すと終了時に二重解放で落ちる
        self._jump_style = _JumpStyle(self.style().objectName() or None)
        self.setStyle(self._jump_style)
        self.setPageStep(0)

class PlaylistList(QListWidget):
    tracks_dropped = pyqtSignal(int, list)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAcceptDrops(True)
        self.setDragDropMode(QAbstractItemView.DragDropMode.DropOnly)

    def _ok(self, ev):
        return ev.mimeData().hasFormat(MIME_TRACKS) or ev.mimeData().hasUrls()

    def dragEnterEvent(self, ev):
        ev.acceptProposedAction() if self._ok(ev) else ev.ignore()

    def dragMoveEvent(self, ev):
        if self._ok(ev) and self.itemAt(ev.position().toPoint()) is not None:
            ev.acceptProposedAction()
        else:
            ev.ignore()

    def dropEvent(self, ev):
        item = self.itemAt(ev.position().toPoint())
        if item is None:
            ev.ignore()
            return
        self.tracks_dropped.emit(item.data(Qt.ItemDataRole.UserRole), _payload_from_mime(ev.mimeData()))
        ev.acceptProposedAction()


class PlaylistItemsView(QTableView):
    tracks_dropped = pyqtSignal(list)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAcceptDrops(True)
        self.setDragDropMode(QAbstractItemView.DragDropMode.DropOnly)

    def dragEnterEvent(self, ev):
        ev.acceptProposedAction() if (ev.mimeData().hasFormat(MIME_TRACKS) or ev.mimeData().hasUrls()) else ev.ignore()

    def dragMoveEvent(self, ev):
        ev.acceptProposedAction()

    def dropEvent(self, ev):
        self.tracks_dropped.emit(_payload_from_mime(ev.mimeData()))
        ev.acceptProposedAction()


def _payload_from_mime(md) -> list[dict]:
    if md.hasFormat(MIME_TRACKS):
        try:
            return json.loads(bytes(md.data(MIME_TRACKS)).decode())
        except ValueError:
            return []
    out = []
    for u in md.urls():
        if u.isLocalFile():
            out.append({"id": None, "path": u.toLocalFile(), "start_frame": 0})
    return out


class LibProvider:
    def __init__(self, win: "MainWindow"):
        self.w = win

    def folder_tracks(self, t):
        # 「フォルダ」= 通常フォルダ、または CUE 1枚(アルバム)
        return self.w.lib.tracks_in_group(t.get("group_key") or t["folder"])

    def all_tracks(self):
        return self.w.all_tracks_cached()


# ------------------------------------------------------------------ メインウィンドウ
class CaptureDialog(QDialog):
    """音を出しているアプリの画面(ウィンドウ/タブ)を一覧し、選んだものの音を Miyamizu の排他出力で再生する。"""
    refresh_requested = pyqtSignal()
    chosen = pyqtSignal(int, str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("アプリの音声を排他出力で再生")
        self.resize(640, 360)
        v = QVBoxLayout(self)
        v.addWidget(QLabel("今音を出しているアプリの画面を選ぶと、その音を Miyamizu 経由で排他出力(DAC 直結)で再生します。\n"
                           "🔊 が付いているものが、今実際に音が出ているストリームです(バーは音量。再生中は動きます)。\n"
                           "同じアプリが複数並ぶ時は、🔊 とバーが動くものを選んでください。排他出力専用/RAM再生・リサンプルは無効です。"))
        self.lst = QListWidget()
        self.lst.setIconSize(QSize(24, 24))
        self.lst.itemDoubleClicked.connect(lambda *_: self._ok())
        v.addWidget(self.lst, 1)
        h = QHBoxLayout()
        b = QPushButton("一覧を更新")
        b.clicked.connect(self.refresh_requested.emit)
        h.addWidget(b)
        h.addStretch(1)
        self.btn = QPushButton("取り込んで再生")
        self.btn.clicked.connect(self._ok)
        h.addWidget(self.btn)
        c = QPushButton("閉じる")
        c.clicked.connect(self.reject)
        h.addWidget(c)
        v.addLayout(h)
        self._timer = QTimer(self)                  # 開いている間は自動で更新(アプリが後から鳴り始めても拾う)
        self._timer.timeout.connect(self.refresh_requested.emit)
        self._timer.start(2000)

    @staticmethod
    def _bar(db) -> str:
        if db is None or db < -70:
            return ""
        n = max(1, min(8, int((db + 60) / 7.5) + 1))
        return " " + "▮" * n + "▯" * (8 - n)

    def set_streams(self, streams: list):
        cur = self.lst.currentItem().data(Qt.ItemDataRole.UserRole) if self.lst.currentItem() else None
        self.lst.clear()
        # 今実際に音が出ているものを上へ(音量の大きい順)。止まっているものは下にグレーで
        order = sorted(streams, key=lambda st: (not st.get("active"), -(st.get("level_db") if st.get("level_db") is not None else -999)))
        first_active = None
        for st in order:
            title = st.get("media") or ""
            name = f"{st['app']}" + (f"  —  {title}" if title and title != st["app"] else "")
            if st.get("active"):
                text = "🔊 " + name + self._bar(st.get("level_db"))
            elif st.get("corked"):
                text = "⏸ " + name + "  (一時停止中)"
            elif st.get("level_db") is None and "active" not in st:
                text = name
            else:
                text = "・ " + name + "  (今は無音)"
            it = QListWidgetItem(QIcon.fromTheme(st.get("icon") or st.get("binary") or "audio-x-generic"), text)
            it.setData(Qt.ItemDataRole.UserRole, (int(st["index"]), st["app"]))
            if not st.get("active") and "active" in st:
                it.setForeground(self.palette().placeholderText())
            self.lst.addItem(it)
            if st.get("active") and first_active is None:
                first_active = it
            if cur and cur[0] == st["index"]:
                self.lst.setCurrentItem(it)
        if not streams:
            self.lst.addItem("(音を出しているアプリがありません)")
        elif self.lst.currentItem() is None:
            self.lst.setCurrentItem(first_active) if first_active is not None else self.lst.setCurrentRow(0)

    def _ok(self):
        it = self.lst.currentItem()
        d = it.data(Qt.ItemDataRole.UserRole) if it else None
        if d:
            self.chosen.emit(d[0], d[1])


def abx_pvalue(correct: int, trials: int) -> float:
    """偶然(当て推量)でこれ以上当たる確率(片側二項検定)。"""
    from math import comb

    if trials <= 0:
        return 1.0
    return sum(comb(trials, k) for k in range(correct, trials + 1)) / 2 ** trials


class ABXDialog(QDialog):
    """出力プロファイル A/B を、同じ曲の同じ位置で切り替えて聴き比べる ABX テスト。
    X は A か B のどちらか(毎回ランダム・非表示)。切り替えには出力の開き直しがあり、数百 ms〜の途切れが入る。"""
    switch_requested = pyqtSignal(str)
    closed = pyqtSignal()

    def __init__(self, names: list, parent=None, rng=None):
        super().__init__(parent)
        import random

        self._rng = rng or random.Random()
        self.setWindowTitle("ABX 聴き比べ")
        self.resize(460, 300)
        v = QVBoxLayout(self)
        v.addWidget(QLabel("同じ曲の同じ位置から、出力プロファイル A と B を切り替えて聴き比べます。\n"
                           "X は毎回ランダムに A か B です。聞き分けられたか、偶然でも当たる確率と一緒に集計します。\n"
                           "※ 切り替え時に出力を開き直すので、短い途切れが入ります。シグナルパス表示で設定が分かってしまうので、見ないで聴いてください。"))
        h = QHBoxLayout()
        self.cmb_a, self.cmb_b = QComboBox(), QComboBox()
        for c in (self.cmb_a, self.cmb_b):
            for n in names:
                c.addItem(n, n)
        if len(names) > 1:
            self.cmb_b.setCurrentIndex(1)
        h.addWidget(QLabel("A:"))
        h.addWidget(self.cmb_a, 1)
        h.addWidget(QLabel("B:"))
        h.addWidget(self.cmb_b, 1)
        v.addLayout(h)
        h = QHBoxLayout()
        self.btn_a, self.btn_b, self.btn_x = QPushButton("A を聴く"), QPushButton("B を聴く"), QPushButton("X を聴く")
        for b in (self.btn_a, self.btn_b, self.btn_x):
            h.addWidget(b)
        v.addLayout(h)
        self.btn_a.clicked.connect(lambda: self._listen("A"))
        self.btn_b.clicked.connect(lambda: self._listen("B"))
        self.btn_x.clicked.connect(lambda: self._listen("X"))
        h = QHBoxLayout()
        h.addWidget(QLabel("X はどちら?"))
        self.ans_a, self.ans_b = QPushButton("X = A"), QPushButton("X = B")
        h.addWidget(self.ans_a)
        h.addWidget(self.ans_b)
        v.addLayout(h)
        self.ans_a.clicked.connect(lambda: self._answer("A"))
        self.ans_b.clicked.connect(lambda: self._answer("B"))
        self.lbl = QLabel("")
        self.lbl.setWordWrap(True)
        v.addWidget(self.lbl)
        h = QHBoxLayout()
        r = QPushButton("集計をリセット")
        r.clicked.connect(self.reset)
        h.addWidget(r)
        h.addStretch(1)
        c = QPushButton("閉じる")
        c.clicked.connect(self.close)
        h.addWidget(c)
        v.addLayout(h)
        self.reset()

    def reset(self):
        self.trials = self.correct = 0
        self._new_trial()
        self._show("")

    def _new_trial(self):
        self.x_is = self._rng.choice("AB")

    def _name(self, which: str) -> str:
        return self.cmb_a.currentData() if which == "A" else self.cmb_b.currentData()

    def _listen(self, which: str):
        real = self.x_is if which == "X" else which
        for b in (self.btn_a, self.btn_b, self.btn_x):
            b.setEnabled(False)
        QTimer.singleShot(1500, lambda: [b.setEnabled(True) for b in (self.btn_a, self.btn_b, self.btn_x)])   # 切り替え中の連打を避ける
        self.switch_requested.emit(self._name(real))

    def _answer(self, guess: str):
        ok = guess == self.x_is
        self.trials += 1
        self.correct += int(ok)
        msg = f"{'正解' if ok else '不正解'}(X は {self.x_is} = {self._name(self.x_is)} でした)"
        self._new_trial()
        self._show(msg)

    def _show(self, last: str):
        p = abx_pvalue(self.correct, self.trials)
        stat = f"{self.correct} / {self.trials} 回正解" + (f"(偶然でこれ以上当たる確率 {p * 100:.1f}%)" if self.trials else "")
        self.lbl.setText((last + "\n" if last else "") + stat + ("\n10回以上で p<5% なら聞き分けられている目安です。" if self.trials else ""))

    def closeEvent(self, e):
        self.closed.emit()
        super().closeEvent(e)


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Miyamizu")
        self.resize(1280, 780)
        self.settings: Settings = load_settings()
        self.qs = QSettings("miyamizu", "miyamizu")
        if not self.qs.allKeys():                                   # 旧名のウィンドウ配置を引き継ぐ
            old = QSettings("hifiplayer", "hifiplayer")
            for k in old.allKeys():
                self.qs.setValue(k, old.value(k))
        self._abx = None
        self.lib = Library()
        self._all_cache: list[dict] | None = None
        self.order = PlayOrder(LibProvider(self))
        self.known: dict = {}               # エンジンの曲 id → GUI 側の曲 dict
        self.cur: dict | None = None
        self.engine_state = "stopped"
        self.engine_hello: dict = {}
        self.seeking = False
        self.cur_dur = 0.0
        self.scan_worker: ScanWorker | None = None
        self._retiring: list[QThread] = []
        self.cover_cache: dict[str, QImage] = {}
        self.cover_key = ""
        self.view_tracks: list[dict] = []
        self.view_dir = ""
        self.view_group: str | None = None       # CUE アルバム表示中はその CUE のキー
        self.view_base: list[dict] = []          # 既定順(アルバム/トラック番号順)
        self.sort_col: int | None = None
        self.sort_desc = False
        self._ctx_view_id = None                 # 再生の文脈になっている表示(ソート時の追従用)
        self._tree_sync = False
        self.recursive = bool(self.qs.value("recursive", True, type=bool))
        self.order.set_modes(self.settings.repeat, self.settings.shuffle)
        self._last_inv_cs = 0
        self._settings_dlg: SettingsDialog | None = None

        self._build_central()
        self._build_docks()
        self._build_menus()
        self._restore_layout()

        self.client = EngineClient(parent=self)
        self.client.ev_received.connect(self.on_event)
        self.client.disconnected.connect(self.on_disconnected)
        ok = self.client.start()
        if not ok:
            QMessageBox.critical(self, "エンジン", f"再生エンジンを起動できませんでした。\nログ: {self.client.log_path}")
        else:
            self.client.send("hello")
        QTimer.singleShot(0, self._startup_library)

    # ---------------------------------------------------------------- UI 構築
    def _build_central(self):
        self.bg = ArtBackground()
        lay = QVBoxLayout(self.bg)
        lay.setContentsMargins(0, 0, 0, 0)
        self.track_model = TrackModel(self)
        self.track_view = QTableView()
        self._style_table(self.track_view, self.track_model)
        hh = self.track_view.horizontalHeader()
        hh.setSectionsClickable(True)
        hh.sectionClicked.connect(self.on_header_clicked)
        hh.setToolTip("クリックで並べ替え(昇順→降順→元の順)")
        self.track_view.setDragEnabled(True)
        self.track_view.setDragDropMode(QAbstractItemView.DragDropMode.DragOnly)
        self.track_view.doubleClicked.connect(lambda idx: self.play_from_view(idx.row()))
        self.track_view.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.track_view.customContextMenuRequested.connect(self.track_menu)
        lay.addWidget(self.track_view)
        self.setCentralWidget(self.bg)

    def _style_table(self, v: QTableView, model: TrackModel):
        v.setModel(model)
        v.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        v.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        v.setShowGrid(False)
        v.setAlternatingRowColors(False)
        v.verticalHeader().setVisible(False)
        v.verticalHeader().setDefaultSectionSize(24)
        v.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        v.setWordWrap(False)
        v.setStyleSheet("QTableView{background:transparent;color:#e6e6e6;selection-background-color:rgba(88,196,160,90);"
                        "selection-color:#ffffff;gridline-color:transparent;}"
                        "QTableView::item{padding:0 6px;}"
                        "QHeaderView::section{background:rgba(20,22,26,200);color:#bbb;border:0;padding:4px 6px;}")
        h = v.horizontalHeader()
        h.setStretchLastSection(True)
        h.setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        for i, w in enumerate([26, 64, 36, 260, 170, 200, 60, 130, 220]):
            v.setColumnWidth(i, w)
        v.viewport().setAutoFillBackground(False)

    def _dock(self, title: str, widget: QWidget, name: str, area=Qt.DockWidgetArea.LeftDockWidgetArea) -> QDockWidget:
        d = QDockWidget(title, self)
        d.setObjectName(name)
        d.setWidget(widget)
        d.setFeatures(QDockWidget.DockWidgetFeature.DockWidgetClosable
                      | QDockWidget.DockWidgetFeature.DockWidgetMovable
                      | QDockWidget.DockWidgetFeature.DockWidgetFloatable)
        self.addDockWidget(area, d)
        return d

    def _build_docks(self):
        # --- ディレクトリツリー(CUE+FLAC は 1 枚ごとに「フォルダ」として子ノードに出す) ---
        self.tree = QTreeWidget()
        self.tree.setHeaderHidden(True)
        self.tree.itemExpanded.connect(self._tree_expand)
        self.tree.currentItemChanged.connect(self._tree_current_changed)
        self.tree.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.tree.customContextMenuRequested.connect(self.tree_menu)
        self.dock_tree = self._dock("ディレクトリ", self.tree, "dock_tree")
        self.tree_root = ""
        self._set_tree_root(self.settings.library_dir or str(Path.home()))

        # --- プレイリスト ---
        pw = QWidget()
        pl = QVBoxLayout(pw)
        pl.setContentsMargins(2, 2, 2, 2)
        top = QHBoxLayout()
        self.btn_new_pl = QPushButton("＋ 新規プレイリスト")
        self.btn_new_pl.clicked.connect(self.new_playlist)
        top.addWidget(self.btn_new_pl)
        pl.addLayout(top)
        self.pl_list = PlaylistList()
        self.pl_list.currentItemChanged.connect(lambda cur, prev: self.show_playlist())
        self.pl_list.tracks_dropped.connect(self.drop_to_playlist)
        self.pl_list.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.pl_list.customContextMenuRequested.connect(self.playlist_menu)
        self.pl_model = TrackModel(self, playlist=True)
        self.pl_view = PlaylistItemsView()
        self._style_table(self.pl_view, self.pl_model)
        self.pl_view.setStyleSheet("")
        self.pl_view.horizontalHeader().setStretchLastSection(True)
        for i, w in enumerate([22, 58, 30, 150, 100, 100, 50, 0, 0]):
            self.pl_view.setColumnWidth(i, w)
        self.pl_view.setColumnHidden(COL_FMT, True)
        self.pl_view.setColumnHidden(COL_FILE, True)
        self.pl_view.doubleClicked.connect(lambda idx: self.play_from_playlist(idx.row()))
        self.pl_view.tracks_dropped.connect(lambda payload: self.drop_to_playlist(self.current_playlist_id(), payload))
        self.pl_view.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.pl_view.customContextMenuRequested.connect(self.playlist_item_menu)
        sp = QSplitter(Qt.Orientation.Vertical)
        sp.addWidget(self.pl_list)
        sp.addWidget(self.pl_view)
        sp.setSizes([120, 280])
        pl.addWidget(sp)
        self.dock_pl = self._dock("プレイリスト", pw, "dock_playlist")
        self.refresh_playlists()

        # --- 再生バー ---
        bar = QWidget()
        bl = QVBoxLayout(bar)
        bl.setContentsMargins(6, 2, 6, 2)
        row1 = QHBoxLayout()
        self.lbl_title = QLabel("停止中")
        self.lbl_title.setStyleSheet("font-weight:bold;")
        self.lbl_title.setMinimumWidth(200)
        row1.addWidget(self.lbl_title, 1)
        self.lbl_path = QLabel("")
        self.lbl_path.setStyleSheet("color:#8a8f98;")
        row1.addWidget(self.lbl_path, 1)
        bl.addLayout(row1)
        row2 = QHBoxLayout()
        st = self.style()

        def tb(icon, tip, slot):
            b = QToolButton()
            b.setIcon(st.standardIcon(icon))
            b.setToolTip(tip)
            b.clicked.connect(slot)
            return b

        self.btn_prev = tb(QStyle.StandardPixmap.SP_MediaSkipBackward, "前の曲", self.prev_track)
        self.btn_play = tb(QStyle.StandardPixmap.SP_MediaPlay, "再生/一時停止 (Space)", self.toggle_play)
        self.btn_stop = tb(QStyle.StandardPixmap.SP_MediaStop, "停止", self.stop)
        self.btn_next = tb(QStyle.StandardPixmap.SP_MediaSkipForward, "次の曲", self.next_track)
        self.btn_release = tb(QStyle.StandardPixmap.SP_DialogCloseButton,
                              ("出力デバイスを解放(排他していた DAC を OS に戻す)" if IS_WIN else "出力デバイスを解放(排他していた DAC を OS/PipeWire に戻す)"), self.release_device)
        for b in (self.btn_prev, self.btn_play, self.btn_stop, self.btn_next, self.btn_release):
            row2.addWidget(b)
        self.cmb_profile = QComboBox()
        self.cmb_profile.setToolTip("出力プロファイル(共有/排他など)。選ぶと出力設定を切り替え、再生中なら同じ位置から再開します")
        self.cmb_profile.setMinimumWidth(150)
        self.cmb_profile.activated.connect(self._profile_activated)
        row2.addWidget(self.cmb_profile)
        self.lbl_pos = QLabel("0:00")
        row2.addWidget(self.lbl_pos)
        self.seek = JumpSlider(Qt.Orientation.Horizontal)
        self.seek.setRange(0, 1000)
        self.seek.sliderPressed.connect(lambda: setattr(self, "seeking", True))
        self.seek.sliderReleased.connect(self.on_seek_released)
        row2.addWidget(self.seek, 1)
        self.lbl_dur = QLabel("0:00")
        row2.addWidget(self.lbl_dur)
        self.btn_repeat = QPushButton()
        self.btn_repeat.clicked.connect(self.cycle_repeat)
        self.btn_shuffle = QPushButton()
        self.btn_shuffle.clicked.connect(self.cycle_shuffle)
        row2.addWidget(self.btn_repeat)
        row2.addWidget(self.btn_shuffle)
        row2.addWidget(QLabel("🔊"))
        self.vol = QSlider(Qt.Orientation.Horizontal)
        self.vol.setRange(0, 100)
        self.vol.setFixedWidth(110)
        self.vol.setValue(int(round(self.settings.volume * 100)))
        self.vol.valueChanged.connect(self.on_volume)
        row2.addWidget(self.vol)
        bl.addLayout(row2)
        self.dock_bar = self._dock("再生バー", bar, "dock_bar", Qt.DockWidgetArea.BottomDockWidgetArea)
        self._update_mode_buttons()
        self._ensure_default_profiles()
        self._refresh_profiles()

        # --- シグナルパス / メトリクス ---
        mw = QWidget()
        ml = QVBoxLayout(mw)
        ml.setContentsMargins(6, 4, 6, 4)
        self.lbl_signal = QLabel("再生するとシグナルパスが表示されます")
        self.lbl_signal.setWordWrap(True)
        self.lbl_signal.setTextFormat(Qt.TextFormat.RichText)
        ml.addWidget(self.lbl_signal)
        ml.addWidget(QLabel("<b>出力周期のずれ(µs)</b>"))
        self.hist = HistogramWidget()
        ml.addWidget(self.hist)
        self.lbl_metrics = QLabel("")
        self.lbl_metrics.setWordWrap(True)
        self.lbl_metrics.setTextFormat(Qt.TextFormat.RichText)
        ml.addWidget(self.lbl_metrics)
        ml.addWidget(QLabel("<b>CPU 周波数</b>"))
        self.freqw = FreqWidget()
        ml.addWidget(self.freqw)
        ml.addStretch(1)
        self.dock_metrics = self._dock("シグナルパス / メトリクス", mw, "dock_metrics", Qt.DockWidgetArea.RightDockWidgetArea)

        # --- 再生キュー(これから再生される順) ---
        qw = QWidget()
        ql = QVBoxLayout(qw)
        ql.setContentsMargins(2, 2, 2, 2)
        self.lbl_queue = QLabel("")
        self.lbl_queue.setStyleSheet("color:#9aa0a8;")
        ql.addWidget(self.lbl_queue)
        self.queue_model = TrackModel(self, queue=True)
        self.queue_view = QTableView()
        self._style_table(self.queue_view, self.queue_model)
        self.queue_view.setStyleSheet("")
        for c in (COL_FMT, COL_FILE):
            self.queue_view.setColumnHidden(c, True)
        for i, w in enumerate([22, 58, 28, 170, 100, 100, 48]):
            self.queue_view.setColumnWidth(i, w)
        self.queue_view.doubleClicked.connect(self.play_from_queue)
        ql.addWidget(self.queue_view)
        self.dock_queue = self._dock("再生キュー", qw, "dock_queue", Qt.DockWidgetArea.RightDockWidgetArea)
        self._apply_ram_column()

    def _build_menus(self):
        mb = self.menuBar()

        def act(menu, text, slot, shortcut=None, checkable=False, checked=False):
            a = QAction(text, self)
            if shortcut:
                a.setShortcut(QKeySequence(shortcut))
            a.setCheckable(checkable)
            if checkable:
                a.setChecked(checked)
                a.toggled.connect(slot)
            else:
                a.triggered.connect(lambda _=False: slot())
            menu.addAction(a)
            return a

        m = mb.addMenu("ファイル(&F)")
        act(m, "ディレクトリを開く…", self.open_dir, "Ctrl+O")
        act(m, "現在のディレクトリを既定にする", self.set_default_dir)
        act(m, "ライブラリを再スキャン", self.rescan)
        m.addSeparator()
        act(m, "M3U8 プレイリストを読み込む…", self.import_m3u)
        act(m, "選択中のプレイリストを M3U8 に書き出す…", self.export_m3u)
        m.addSeparator()
        act(m, "終了", self.close, "Ctrl+Q")

        m = mb.addMenu("再生(&P)")
        act(m, "再生 / 一時停止", self.toggle_play, "Space")
        act(m, "停止", self.stop, "Ctrl+.")
        act(m, "前の曲", self.prev_track, "Ctrl+Left")
        act(m, "次の曲", self.next_track, "Ctrl+Right")
        m.addSeparator()
        if not IS_WIN:      # アプリ音声の取り込みは PipeWire/pactl 前提(Windows 版は未対応)
            act(m, "アプリの音声を排他出力で再生…", self.open_capture_dialog)
        act(m, "静音モード(再生中の裏方の動きを減らす)", self._toggle_quiet, checkable=True, checked=self.settings.quiet_mode)
        if not IS_WIN:
            m.addSeparator()
            act(m, "アプリ音声の再生を停止", lambda: self.client.send("capture_stop"))
        m.addSeparator()
        act(m, "出力デバイスを解放(DAC をOSに戻す)", self.release_device)
        sub = m.addMenu("出力プロファイル")
        sub.addAction("現在の出力設定をプロファイルとして保存…", self.save_profile)
        sub.addAction("プロファイルを削除…", self.delete_profile)
        m.addSeparator()
        sub = m.addMenu("リピート")
        g = QActionGroup(self)
        self.repeat_actions = {}
        for key, label in REPEAT_MODES:
            a = QAction(label.split(": ")[1], self, checkable=True)
            a.triggered.connect(lambda _=False, k=key: self.set_repeat(k))
            g.addAction(a)
            sub.addAction(a)
            self.repeat_actions[key] = a
        sub = m.addMenu("シャッフル")
        g2 = QActionGroup(self)
        self.shuffle_actions = {}
        for key, label in SHUFFLE_MODES:
            a = QAction(label.split(": ")[1], self, checkable=True)
            a.triggered.connect(lambda _=False, k=key: self.set_shuffle(k))
            g2.addAction(a)
            sub.addAction(a)
            self.shuffle_actions[key] = a

        m = mb.addMenu("表示(&V)")
        for d in (self.dock_tree, self.dock_pl, self.dock_queue, self.dock_bar, self.dock_metrics):
            m.addAction(d.toggleViewAction())
        m.addSeparator()
        act(m, "選択フォルダのサブフォルダも表示", self.toggle_recursive, checkable=True, checked=self.recursive)
        act(m, "並び順を元に戻す(アルバム/トラック番号順)", self.reset_sort)
        act(m, "レイアウトを初期状態に戻す", self.reset_layout)
        self.menu_debug = mb.addMenu("デバッグ(&D)")          # 設定 → その他 で有効にした時だけ表示
        act(self.menu_debug, "ABX 聴き比べ(出力プロファイル A/B)…", self.open_abx_dialog)
        self.menu_debug.menuAction().setVisible(bool(self.settings.debug_enabled))
        m = mb.addMenu("設定(&S)")
        act(m, "設定…", self.open_settings, "Ctrl+,")
        m = mb.addMenu("ヘルプ(&H)")
        act(m, "診断レポートをクリップボードにコピー", self.copy_diagnostics)
        act(m, "バージョン情報", self.show_about)
        self._update_mode_buttons()

    # ---------------------------------------------------------------- レイアウト
    def reset_layout(self):
        for d in (self.dock_tree, self.dock_pl, self.dock_queue, self.dock_bar, self.dock_metrics):
            d.setFloating(False)
            d.show()
        self.addDockWidget(Qt.DockWidgetArea.LeftDockWidgetArea, self.dock_tree)
        self.addDockWidget(Qt.DockWidgetArea.LeftDockWidgetArea, self.dock_pl)
        self.addDockWidget(Qt.DockWidgetArea.BottomDockWidgetArea, self.dock_bar)
        self.addDockWidget(Qt.DockWidgetArea.RightDockWidgetArea, self.dock_metrics)
        self.addDockWidget(Qt.DockWidgetArea.RightDockWidgetArea, self.dock_queue)
        self.tabifyDockWidget(self.dock_queue, self.dock_metrics)
        self.dock_queue.raise_()
        self.splitDockWidget(self.dock_tree, self.dock_pl, Qt.Orientation.Vertical)
        self.resizeDocks([self.dock_tree, self.dock_pl], [320, 380], Qt.Orientation.Vertical)
        self.resizeDocks([self.dock_tree, self.dock_queue], [250, 340], Qt.Orientation.Horizontal)

    def _restore_layout(self):
        self.reset_layout()
        geo = self.qs.value("geometry")
        state = self.qs.value("state")
        if isinstance(geo, QByteArray):
            self.restoreGeometry(geo)
        # ドック構成を変えた版では古い保存状態を使わない(新しいドックが消えるのを防ぐ)
        if isinstance(state, QByteArray) and str(self.qs.value("layout_ver", "")) == LAYOUT_VER:
            self.restoreState(state)

    # ---------------------------------------------------------------- ライブラリ
    def all_tracks_cached(self) -> list[dict]:
        if self._all_cache is None:
            self._all_cache = self.lib.all_tracks(False)
        return self._all_cache

    # ---- ディレクトリツリー ----
    def _new_tree_item(self, text: str, kind: str, key: str, expandable: bool) -> QTreeWidgetItem:
        it = QTreeWidgetItem([text])
        it.setData(0, Qt.ItemDataRole.UserRole, (kind, key))
        it.setToolTip(0, key)
        if expandable:
            QTreeWidgetItem(it, ["…"]).setData(0, Qt.ItemDataRole.UserRole, ("placeholder", ""))
        return it

    def _set_tree_root(self, path: str):
        if not os.path.isdir(path):
            path = str(Path.home())
        self.tree_root = path
        self._tree_sync = True
        self.tree.clear()
        root = self._new_tree_item(path, "dir", path, True)
        self.tree.addTopLevelItem(root)
        root.setExpanded(True)       # itemExpanded で子を作る
        self._tree_sync = False

    def _tree_fill(self, item: QTreeWidgetItem):
        kind, key = item.data(0, Qt.ItemDataRole.UserRole) or ("", "")
        if kind != "dir":
            return
        item.takeChildren()
        try:
            names = sorted((e.name for e in os.scandir(key) if e.is_dir(follow_symlinks=True) and not e.name.startswith(".")),
                           key=natural_key)
        except OSError:
            names = []
        for n in names:
            item.addChild(self._new_tree_item(n, "dir", os.path.join(key, n), True))
        # CUE+FLAC は 1 枚 = 1 フォルダ
        for g in self.lib.cue_groups(key):
            label = "💿 " + (g.get("album") or Path(g["group_key"]).stem)
            item.addChild(self._new_tree_item(label, "grp", g["group_key"], False))

    def _tree_expand(self, item: QTreeWidgetItem):
        if item.childCount() == 1 and (item.child(0).data(0, Qt.ItemDataRole.UserRole) or ("",))[0] == "placeholder":
            self._tree_fill(item)

    def _tree_current_changed(self, cur, prev):
        if self._tree_sync or cur is None:
            return
        kind, key = cur.data(0, Qt.ItemDataRole.UserRole) or ("", "")
        if kind == "dir":
            self.show_dir(key, from_tree=True)
        elif kind == "grp":
            self.show_group(key, from_tree=True)

    def _expanded_dirs(self) -> set[str]:
        out: set[str] = set()

        def walk(it):
            if it.isExpanded():
                k = it.data(0, Qt.ItemDataRole.UserRole)
                if k and k[0] == "dir":
                    out.add(k[1])
            for i in range(it.childCount()):
                walk(it.child(i))
        for i in range(self.tree.topLevelItemCount()):
            walk(self.tree.topLevelItem(i))
        return out

    def _refresh_tree(self):
        """スキャン後にツリー(CUE ノード含む)を作り直し、展開と選択を戻す。"""
        expanded = self._expanded_dirs()
        sel = self.view_group or self.view_dir
        self._tree_sync = True
        self.tree.clear()
        root = self._new_tree_item(self.tree_root, "dir", self.tree_root, True)
        self.tree.addTopLevelItem(root)

        def restore(it):
            k = it.data(0, Qt.ItemDataRole.UserRole)
            if k and k[0] == "dir" and (k[1] in expanded or k[1] == self.tree_root):
                self._tree_fill(it)
                it.setExpanded(True)
                for i in range(it.childCount()):
                    restore(it.child(i))
        restore(root)
        self._tree_sync = False
        if sel:
            self._select_in_tree(sel)

    def _find_child(self, item: QTreeWidgetItem, key: str):
        for i in range(item.childCount()):
            c = item.child(i)
            k = c.data(0, Qt.ItemDataRole.UserRole)
            if k and k[1] == key:
                return c
        return None

    def _select_in_tree(self, key: str, is_group: bool = False):
        """key(フォルダ or CUE のパス)までツリーを展開して選択する。"""
        root = self.tree.topLevelItem(0)
        if root is None:
            return
        is_group = is_group or (self.view_group is not None and key == self.view_group)
        folder = str(Path(key).parent) if is_group else key
        if folder != self.tree_root and not folder.startswith(self.tree_root.rstrip("/") + "/"):
            return
        self._tree_sync = True
        try:
            item = root
            root.setExpanded(True)
            rel = os.path.relpath(folder, self.tree_root)
            for part in ([] if rel == "." else rel.split(os.sep)):
                self._tree_expand(item)
                item.setExpanded(True)
                nxt = self._find_child(item, os.path.join((item.data(0, Qt.ItemDataRole.UserRole) or ("", ""))[1], part))
                if nxt is None:
                    break
                item = nxt
            if is_group:
                self._tree_expand(item)
                item.setExpanded(True)
                item = self._find_child(item, key) or item
            self.tree.setCurrentItem(item)
            self.tree.scrollToItem(item)
        finally:
            self._tree_sync = False

    def _startup_library(self):
        d = self.settings.library_dir
        if d and os.path.isdir(d):
            self._refresh_tree()
            self.show_dir(d)
            self.start_scan(d)

    def start_scan(self, root: str, reset: bool = False):
        if self.scan_worker is not None and self.scan_worker.isRunning():
            self.scan_worker.cancel()
            self._retiring.append(self.scan_worker)
            self.scan_worker.finished.connect(lambda w=self.scan_worker: self._retiring.remove(w) if w in self._retiring else None)
        w = ScanWorker(root, self, exts=active_exts(self.settings), reset=reset)
        w.progress.connect(lambda d, n: self.statusBar().showMessage(f"スキャン中… {n} ファイル  {d}"))
        w.done.connect(lambda n, r=root: self.on_scan_done(r, n))
        self.scan_worker = w
        w.start()

    def on_scan_done(self, root: str, n: int):
        self._all_cache = None
        self.statusBar().showMessage(f"スキャン完了: {self.lib.count()} 曲 (更新フォルダ {n})", 6000)
        self._refresh_tree()
        self._reload_view()
        self.show_playlist()

    def rescan(self):
        d = self.settings.library_dir or self.view_dir
        if d:
            self.start_scan(d)

    def open_dir(self):
        d = QFileDialog.getExistingDirectory(self, "ディレクトリを開く", self.view_dir or self.settings.library_dir or str(Path.home()))
        if d:
            if not (d == self.tree_root or d.startswith(self.tree_root.rstrip("/") + "/")):
                self._set_tree_root(d)
            self.show_dir(d)
            self.start_scan(d)

    def set_default_dir(self):
        if self.view_dir:
            self.settings.library_dir = self.view_dir
            save_settings(self.settings)
            self._set_tree_root(self.view_dir)
            self.start_scan(self.view_dir)
            self.show_dir(self.view_dir)

    def _reload_view(self):
        if self.view_group:
            self.show_group(self.view_group)
        elif self.view_dir:
            self.show_dir(self.view_dir)

    def _view_id(self):
        return (self.view_dir, self.view_group, self.recursive)

    def _set_view_tracks(self, tracks: list[dict]):
        self.view_base = tracks
        self._apply_sort()

    def show_dir(self, path: str, from_tree: bool = False):
        self.view_dir = path
        self.view_group = None
        self._set_view_tracks(self.lib.tracks_in_dir(path, self.recursive, self.settings.show_cue_source))
        self.statusBar().showMessage(f"{path}  ({len(self.view_tracks)} 曲)", 5000)
        if not from_tree:
            self._select_in_tree(path)

    def show_group(self, key: str, from_tree: bool = False):
        """CUE+FLAC 1 枚(1 アルバム)を 1 つのフォルダとして表示する。"""
        self.view_group = key
        self.view_dir = str(Path(key).parent)
        self._set_view_tracks(self.lib.tracks_in_group(key))
        self.statusBar().showMessage(f"💿 {Path(key).name}  ({len(self.view_tracks)} 曲)", 5000)
        if not from_tree:
            self._select_in_tree(key, is_group=True)

    def toggle_recursive(self, on: bool):
        self.recursive = on
        self.qs.setValue("recursive", on)
        self._reload_view()

    # ---- 並べ替え ----
    _SORT_KEYS = {
        COL_NO: lambda t: (t.get("track_no") if t.get("track_no") is not None else 10 ** 9),
        COL_TITLE: lambda t: natural_key(t.get("title") or ""),
        COL_ARTIST: lambda t: natural_key(t.get("artist") or ""),
        COL_ALBUM: lambda t: natural_key(t.get("album") or ""),
        COL_DUR: lambda t: t.get("duration") or 0.0,
        COL_FMT: lambda t: (t.get("rate") or 0, t.get("bits") or 0, t.get("codec") or ""),
        COL_FILE: lambda t: natural_key(Path(t.get("path", "")).name),
    }

    def on_header_clicked(self, col: int):
        if col not in self._SORT_KEYS:
            return
        if self.sort_col == col:
            if not self.sort_desc:
                self.sort_desc = True
            else:
                self.sort_col, self.sort_desc = None, False      # 3 回目で元の順へ
        else:
            self.sort_col, self.sort_desc = col, False
        self._apply_sort(user=True)

    def reset_sort(self):
        self.sort_col, self.sort_desc = None, False
        self._apply_sort(user=True)

    def _apply_sort(self, user: bool = False):
        h = self.track_view.horizontalHeader()
        if self.sort_col is None:
            self.view_tracks = list(self.view_base)          # 既定順: フォルダ → CUE/アルバム → トラック番号
            h.setSortIndicatorShown(False)
        else:
            key = self._SORT_KEYS[self.sort_col]
            self.view_tracks = sorted(self.view_base, key=key, reverse=self.sort_desc)
            h.setSortIndicatorShown(True)
            h.setSortIndicator(self.sort_col, Qt.SortOrder.DescendingOrder if self.sort_desc else Qt.SortOrder.AscendingOrder)
        self.track_model.set_tracks(self.view_tracks)
        if user and self._ctx_view_id == self._view_id() and not self.order.playlist_mode:
            self.order.set_context(self.view_tracks, playlist=False)      # 再生順も表示順に追従
            self.order.set_modes(self.settings.repeat, self.settings.shuffle)
            self._refresh_queue()

    def tree_menu(self, pos: QPoint):
        it = self.tree.itemAt(pos)
        if it is None:
            return
        kind, path = it.data(0, Qt.ItemDataRole.UserRole) or ("", "")
        m = QMenu(self)
        if kind == "grp":
            tracks = self.lib.tracks_in_group(path)
            m.addAction("このアルバムを再生", lambda: (self.show_group(path), self.play_from_view(0)))
            sub = m.addMenu("プレイリストに追加")
            for p in self.lib.playlists():
                sub.addAction(p["name"], lambda pid=p["id"]: self._add_tracks_to(pid, tracks))
            m.addAction("ファイルの場所を開く", lambda: QDesktopServices.openUrl(QUrl.fromLocalFile(str(Path(path).parent))))
        elif kind == "dir":
            m.addAction("この場所を既定ディレクトリにする", lambda: (self.show_dir(path), self.set_default_dir()))
            m.addAction("このフォルダを再スキャン", lambda: self.start_scan(path))
            m.addAction("ファイルマネージャで開く", lambda: QDesktopServices.openUrl(QUrl.fromLocalFile(path)))
        else:
            return
        m.exec(self.tree.viewport().mapToGlobal(pos))

    # ---------------------------------------------------------------- プレイリスト
    def refresh_playlists(self, select: int | None = None):
        cur = select if select is not None else self.current_playlist_id()
        self.pl_list.blockSignals(True)
        self.pl_list.clear()
        sel_item = None
        for p in self.lib.playlists():
            it = QListWidgetItem(f"{p['name']}  ({p['n']})")
            it.setData(Qt.ItemDataRole.UserRole, p["id"])
            self.pl_list.addItem(it)
            if p["id"] == cur:
                sel_item = it
        if sel_item is not None:
            self.pl_list.setCurrentItem(sel_item)
        self.pl_list.blockSignals(False)
        self.show_playlist()

    def current_playlist_id(self) -> int | None:
        it = self.pl_list.currentItem()
        return it.data(Qt.ItemDataRole.UserRole) if it else None

    def show_playlist(self):
        pid = self.current_playlist_id()
        self.pl_tracks = self.lib.playlist_tracks(pid) if pid is not None else []
        self.pl_model.set_tracks(self.pl_tracks)

    def new_playlist(self):
        name, ok = QInputDialog.getText(self, "新規プレイリスト", "名前:")
        if ok:
            pid = self.lib.create_playlist(name)
            self.refresh_playlists(pid)

    def playlist_menu(self, pos: QPoint):
        it = self.pl_list.itemAt(pos)
        if it is None:
            return
        pid = it.data(Qt.ItemDataRole.UserRole)
        m = QMenu(self)
        m.addAction("名前を変更…", lambda: self._rename_pl(pid, it))
        m.addAction("M3U8 に書き出し…", lambda: self._export_pl(pid))
        m.addAction("削除", lambda: self._delete_pl(pid))
        m.exec(self.pl_list.viewport().mapToGlobal(pos))

    def _rename_pl(self, pid, it):
        name, ok = QInputDialog.getText(self, "名前を変更", "名前:", text=it.text().rsplit("  (", 1)[0])
        if ok and name.strip():
            self.lib.rename_playlist(pid, name)
            self.refresh_playlists(pid)

    def _delete_pl(self, pid):
        if QMessageBox.question(self, "削除", "このプレイリストを削除しますか?(曲ファイルは消えません)") == QMessageBox.StandardButton.Yes:
            self.lib.delete_playlist(pid)
            self.refresh_playlists()

    def _export_pl(self, pid):
        path, _ = QFileDialog.getSaveFileName(self, "M3U8 に書き出し", "playlist.m3u8", "M3U8 (*.m3u8)")
        if path:
            n = self.lib.export_m3u8(pid, path)
            self.statusBar().showMessage(f"{n} 曲を書き出しました", 5000)

    def export_m3u(self):
        pid = self.current_playlist_id()
        if pid is not None:
            self._export_pl(pid)

    def import_m3u(self):
        path, _ = QFileDialog.getOpenFileName(self, "M3U8 を読み込む", str(Path.home()), "プレイリスト (*.m3u *.m3u8)")
        if path:
            self.lib.import_m3u8(path)
            self.refresh_playlists()

    def drop_to_playlist(self, pid, payload: list[dict]):
        if pid is None or not payload:
            return
        tracks = []
        for p in payload:
            t = None
            if p.get("id") is not None:
                t = self.lib.track_by_id(p["id"])
            if t is None:
                t = self.lib.track_by_path(p["path"], p.get("start_frame", 0))
            if t is not None:
                tracks.append(t)
        if tracks:
            self.lib.add_to_playlist(pid, tracks)
            self.refresh_playlists(pid)
            self.statusBar().showMessage(f"{len(tracks)} 曲をプレイリストに追加しました", 4000)

    def selected_tracks(self, view: QTableView, model: TrackModel) -> list[dict]:
        rows = sorted({i.row() for i in view.selectionModel().selectedRows()})
        return [model.tracks[r] for r in rows]

    def track_menu(self, pos: QPoint):
        tracks = self.selected_tracks(self.track_view, self.track_model)
        if not tracks:
            return
        m = QMenu(self)
        m.addAction("再生", lambda: self.play_from_view(self.track_view.currentIndex().row()))
        sub = m.addMenu("プレイリストに追加")
        for p in self.lib.playlists():
            sub.addAction(p["name"], lambda pid=p["id"]: self._add_tracks_to(pid, tracks))
        sub.addSeparator()
        sub.addAction("新しいプレイリスト…", lambda: self._add_tracks_new(tracks))
        m.addAction("ファイルの場所を開く", lambda: QDesktopServices.openUrl(QUrl.fromLocalFile(str(Path(tracks[0]["path"]).parent))))
        m.exec(self.track_view.viewport().mapToGlobal(pos))

    def _add_tracks_to(self, pid, tracks):
        self.lib.add_to_playlist(pid, tracks)
        self.refresh_playlists(pid)

    def _add_tracks_new(self, tracks):
        name, ok = QInputDialog.getText(self, "新規プレイリスト", "名前:")
        if ok:
            pid = self.lib.create_playlist(name)
            self._add_tracks_to(pid, tracks)

    def playlist_item_menu(self, pos: QPoint):
        pid = self.current_playlist_id()
        rows = sorted({i.row() for i in self.pl_view.selectionModel().selectedRows()})
        if pid is None or not rows:
            return
        m = QMenu(self)
        m.addAction("再生", lambda: self.play_from_playlist(rows[0]))
        m.addAction("上へ", lambda: self._move_items(pid, rows, -1))
        m.addAction("下へ", lambda: self._move_items(pid, rows, +1))
        m.addAction("プレイリストから削除", lambda: self._remove_items(pid, rows))
        m.exec(self.pl_view.viewport().mapToGlobal(pos))

    def _remove_items(self, pid, rows):
        self.lib.remove_playlist_items([self.pl_tracks[r]["item_id"] for r in rows])
        self.refresh_playlists(pid)

    def _move_items(self, pid, rows, d):
        for r in (rows if d < 0 else reversed(rows)):
            new = r + d
            if 0 <= new < len(self.pl_tracks):
                self.lib.move_playlist_item(pid, self.pl_tracks[r]["item_id"], new)
                self.pl_tracks = self.lib.playlist_tracks(pid)
        self.show_playlist()

    # ---------------------------------------------------------------- 再生制御
    def engine_track(self, t: dict) -> dict:
        end = t.get("end_frame")
        if end and self.settings.cue_skip_pregap and t.get("end_nopregap"):
            end = t["end_nopregap"]
        return {"id": t["id"], "track_id": t.get("track_id", t.get("id")), "path": t["path"],
                "start_frame": max(0, t.get("start_frame") or 0), "end_frame": end,
                "title": t.get("title"), "artist": t.get("artist"), "album": t.get("album")}

    def play_from_view(self, row: int):
        if row < 0 or row >= len(self.view_tracks):
            return
        self.order.set_context(self.view_tracks, playlist=False)
        self.order.set_modes(self.settings.repeat, self.settings.shuffle)
        self._ctx_view_id = self._view_id()
        self.start_track(self.view_tracks[row])

    def play_from_playlist(self, row: int):
        if row < 0 or row >= len(self.pl_tracks):
            return
        ctx = []
        for t in self.pl_tracks:
            if t.get("missing"):
                continue
            c = dict(t)
            c["track_id"] = t.get("id")
            c["id"] = f"p{t['item_id']}"
            ctx.append(c)
        target = f"p{self.pl_tracks[row]['item_id']}"
        sel = next((c for c in ctx if c["id"] == target), None)
        if sel is None:
            return
        self.order.set_context(ctx, playlist=True)
        self.order.set_modes(self.settings.repeat, self.settings.shuffle)
        self._ctx_view_id = None
        self.start_track(sel)

    def start_track(self, t: dict):
        self.known[t["id"]] = t
        self.cur = t
        self.order.on_started(t)
        self.client.send("set_next", track=None)
        self.client.send("load", track=self.engine_track(t), start=0.0, play=True)
        self._mark_current(t)
        self._refresh_queue()

    def toggle_play(self):
        if self.engine_state == "playing":
            self.client.send("pause")
        elif self.engine_state == "paused":
            self.client.send("resume")
        else:
            idx = self.track_view.currentIndex()
            row = idx.row() if idx.isValid() else 0
            self.play_from_view(row)

    def stop(self):
        self.client.send("stop")

    def next_track(self):
        nxt = self.order.plan_next(self.cur, auto=False)
        if nxt is None:
            self.stop()
            return
        self.start_track(nxt)

    def prev_track(self):
        if self.cur is None:
            return
        pos = getattr(self, "_pos", 0.0)
        if pos > 3.0:
            self.client.send("seek", pos=0.0)
            return
        p = self.order.prev(self.cur)
        if p is not None:
            self.start_track(p)

    def on_seek_released(self):
        self.seeking = False
        if self.cur_dur > 0:
            self.client.send("seek", pos=self.seek.value() / 1000.0 * self.cur_dur)

    def on_volume(self, v: int):
        self.settings.volume = v / 100.0
        self.client.send("volume", v=v / 100.0)

    def cycle_repeat(self):
        keys = [k for k, _ in REPEAT_MODES]
        self.set_repeat(keys[(keys.index(self.settings.repeat) + 1) % len(keys)])

    def cycle_shuffle(self):
        keys = [k for k, _ in SHUFFLE_MODES]
        self.set_shuffle(keys[(keys.index(self.settings.shuffle) + 1) % len(keys)])

    def set_repeat(self, k: str):
        self.settings.repeat = k
        self._modes_changed()

    def set_shuffle(self, k: str):
        self.settings.shuffle = k
        self._modes_changed()

    def _modes_changed(self):
        self.order.set_modes(self.settings.repeat, self.settings.shuffle)
        save_settings(self.settings)
        self._update_mode_buttons()
        if self.cur is not None:      # 次曲の予定を作り直す
            self._send_next(self.cur)
        self._refresh_queue()

    def _update_mode_buttons(self):
        r = dict(REPEAT_MODES)[self.settings.repeat]
        s = dict(SHUFFLE_MODES)[self.settings.shuffle]
        if hasattr(self, "btn_repeat"):
            self.btn_repeat.setText(r)
            self.btn_shuffle.setText(s)
            self.btn_repeat.setStyleSheet("" if self.settings.repeat == "off" else "color:#58c4a0;")
            self.btn_shuffle.setStyleSheet("" if self.settings.shuffle == "off" else "color:#58c4a0;")
        for k, a in getattr(self, "repeat_actions", {}).items():
            a.setChecked(k == self.settings.repeat)
        for k, a in getattr(self, "shuffle_actions", {}).items():
            a.setChecked(k == self.settings.shuffle)

    def _send_next(self, cur: dict):
        nxt = self.order.plan_next(cur, auto=True)
        self._send_ahead(cur)
        if nxt is None:
            self.client.send("set_next", track=None)
            return
        self.known[nxt["id"]] = nxt
        self.client.send("set_next", track=self.engine_track(nxt))

    def _send_ahead(self, cur: dict):
        """実験: 次曲より先の曲(事前変換の対象)をエンジンへ知らせる。"""
        n = self.settings.prerender_ahead if (self.settings.ram_playback and self.settings.prefetch) else 0
        if n <= 0:
            self.client.send("set_ahead", tracks=[])
            return
        up, _ = self.order.upcoming(cur, n + 1)
        ahead = up[1:1 + n] if len(up) > 1 else []
        for t in ahead:
            self.known[t["id"]] = t
        self.client.send("set_ahead", tracks=[self.engine_track(t) for t in ahead])

    # ---- 再生キュー ----
    QUEUE_MAX = 60

    def _refresh_queue(self):
        cur = self.cur
        if cur is None:
            self.queue_model.set_tracks([])
            self.lbl_queue.setText("再生すると、これから再生される順に並びます")
            return
        up, note = self.order.upcoming(cur, self.QUEUE_MAX)
        self.queue_model.set_tracks([cur] + up)
        self.queue_model.set_current(cur.get("id"), cur.get("track_id"))
        self.lbl_queue.setText(f"{note} / 残り {len(up)} 曲" + ("以上" if len(up) >= self.QUEUE_MAX else ""))
        self._push_ram_to_models()

    def play_from_queue(self, idx: QModelIndex):
        row = idx.row()
        if row <= 0 or row >= len(self.queue_model.tracks):
            return
        self.start_track(self.queue_model.tracks[row])       # 文脈(再生リスト)はそのまま、そこから続きを再生

    # ---- RAM 再生の表示 ----
    def _apply_ram_column(self):
        on = bool(self.settings.ram_playback)
        for v in (self.track_view, self.pl_view, self.queue_view):
            v.setColumnHidden(COL_RAM, not on)

    def _on_ram(self, ram: list):
        by_key: dict = {}
        by_track: dict = {}
        for r in ram:
            info = {"done": bool(r.get("done")), "pct": float(r.get("pct", 0.0)), "mb": r.get("mb", 0), "role": r.get("role")}
            by_key[r.get("id")] = info
            t = self.known.get(r.get("id"))
            if t is not None and t.get("track_id") is not None:
                by_track[t["track_id"]] = info
        self._ram_maps = (by_key, by_track)
        self._push_ram_to_models()

    def _push_ram_to_models(self):
        by_key, by_track = getattr(self, "_ram_maps", ({}, {}))
        for m in (self.track_model, self.pl_model, self.queue_model):
            m.set_ram(by_key, by_track)

    def _ram_summary(self, ram: list) -> str:
        if not self.settings.ram_playback:
            return ""
        if not ram:
            return "<br>RAM再生: 待機中(再生を始めると曲をRAMへ展開します)"
        parts = []
        for r in ram:
            who = "再生中" if r.get("role") == "cur" else "次曲(先読み)"
            t = self.known.get(r.get("id")) or {}
            name = (t.get("title") or "")[:24]
            st = f"✔ RAM {r.get('mb')} MB" if r.get("done") else f"読み込み中 {int(float(r.get('pct', 0)) * 100)}%"
            parts.append(f"{who} 「{name}」 {st}")
        return "<br>RAM再生: " + " / ".join(parts)

    def _mark_current(self, t: dict | None):
        key = t["id"] if t else None
        tid = (t.get("track_id") if t and t.get("track_id") is not None else (t.get("id") if t else None))
        self.track_model.set_current(key, tid)
        self.pl_model.set_current(key, tid)

    # ---------------------------------------------------------------- エンジンのイベント
    def on_event(self, ev: dict):
        k = ev.get("ev")
        if k == "status":
            self._on_status(ev)
        elif k == "track_changed":
            self._on_track_changed(ev)
        elif k == "need_next":
            cur = self.known.get(ev.get("id"))
            if cur is not None:
                self._send_next(cur)
        elif k == "finished":
            cur = self.known.get(ev.get("id")) or self.cur
            nxt = self.order.plan_next(cur, auto=True) if cur else None
            if nxt is not None and self.engine_state != "paused":
                self.start_track(nxt)
            else:
                self.statusBar().showMessage("再生が終了しました", 4000)
        elif k == "metrics":
            self._on_metrics(ev)
        elif k == "streams":
            if getattr(self, "_cap_dlg", None) is not None:
                self._cap_dlg.set_streams(ev.get("streams") or [])
        elif k == "capture_started":
            self.statusBar().showMessage(f"🎧 「{ev.get('label')}」の音声をプレイヤー経由で再生中(RAM再生/リサンプルは無効)", 8000)
            self.lbl_title.setText(f"🎧 {ev.get('label')}")
            self.lbl_path.setText("アプリ音声入力")
        elif k == "capture_stopped":
            self.statusBar().showMessage("アプリ音声の取り込みを終了しました(アプリの出力先は元に戻しました)", 6000)
        elif k == "error":
            self.statusBar().showMessage("⚠ " + ev.get("msg", ""), 12000)
        elif k == "notice":
            self.statusBar().showMessage(ev.get("msg", ""), 6000)
        elif k == "hello":
            self.engine_hello = ev
            self._apply_gui_confine()
            self.client.send("configure", settings=self.settings.to_dict())
            self.client.send("volume", v=self.settings.volume)
        elif k == "device_caps":
            if self._settings_dlg is not None:
                self._settings_dlg.show_caps(ev)
        elif k == "assignment":
            self.statusBar().showMessage(("✔ " if ev.get("ok") else "⚠ ") + str(ev.get("msg", "")), 8000)
        elif k == "device_closed":
            why = ev.get("reason")
            self.statusBar().showMessage("出力デバイスを解放しました" + (f"({why})" if why else "(停止中のため)"), 5000)

    # ---- 出力プロファイル ----
    def _ensure_default_profiles(self):
        if not self.settings.output_profiles:
            self.settings.output_profiles = ({"共有 (WASAPI)": {"backend": "pipewire"}, "排他 (WASAPI)": {"backend": "alsa_hw"}}
                                             if IS_WIN else
                                             {"共有 (PipeWire)": {"backend": "pipewire"}, "排他 (ALSA hw)": {"backend": "alsa_hw"}})
            save_settings(self.settings)

    def _current_profile_name(self) -> str | None:
        for name, prof in self.settings.output_profiles.items():
            if prof and all(getattr(self.settings, k, None) == v for k, v in prof.items() if k in PROFILE_KEYS):
                return name
        return None

    def _refresh_profiles(self):
        self.cmb_profile.blockSignals(True)
        self.cmb_profile.clear()
        cur = self._current_profile_name()
        if getattr(self, "_abx", None) is not None:          # ABX 中は名前を伏せる
            self.cmb_profile.addItem("出力: (ABX テスト中)", None)
            self.cmb_profile.setEnabled(False)
            self.cmb_profile.blockSignals(False)
            return
        self.cmb_profile.setEnabled(True)
        for name in self.settings.output_profiles:
            self.cmb_profile.addItem("出力: " + name, name)
        if cur is None:
            self.cmb_profile.addItem("出力: (カスタム)", None)
            self.cmb_profile.setCurrentIndex(self.cmb_profile.count() - 1)
        else:
            self.cmb_profile.setCurrentIndex(self.cmb_profile.findData(cur))
        self.cmb_profile.blockSignals(False)

    def _profile_activated(self, idx: int):
        name = self.cmb_profile.itemData(idx)
        if name:
            self.apply_profile(name)

    def apply_profile(self, name: str, silent: bool = False):
        prof = self.settings.output_profiles.get(name)
        if not prof:
            return
        for k, v in prof.items():
            if k in PROFILE_KEYS and hasattr(self.settings, k):
                setattr(self.settings, k, v)
        save_settings(self.settings)
        resume = self.cur is not None and self.engine_state in ("playing", "paused")
        pos, was_playing = getattr(self, "_pos", 0.0), self.engine_state == "playing"
        self.client.send("configure", settings=self.settings.to_dict())     # 旧デバイスはエンジンが解放する
        self.vol.setEnabled(self.settings.backend != "alsa_hw")
        self.vol.setToolTip("ビットパーフェクト(ALSA hw)では音量は0dB固定です" if self.settings.backend == "alsa_hw" else "")
        self._apply_ram_column()
        self._refresh_profiles()
        if resume:      # 同じ位置から再開(新しい出力で開き直す。使用中なら自動で解放を試みる)
            self.client.send("load", track=self.engine_track(self.cur), start=pos, play=was_playing)
            self._send_next(self.cur)
        if not silent:
            self.statusBar().showMessage(f"出力プロファイル「{name}」に切り替えました", 5000)

    def save_profile(self):
        name, ok = QInputDialog.getText(self, "出力プロファイルを保存", "名前:", text=self._current_profile_name() or "")
        name = name.strip()
        if ok and name:
            self.settings.output_profiles[name] = {k: getattr(self.settings, k) for k in PROFILE_KEYS}
            save_settings(self.settings)
            self._refresh_profiles()

    def delete_profile(self):
        names = list(self.settings.output_profiles)
        if not names:
            return
        name, ok = QInputDialog.getItem(self, "出力プロファイルを削除", "削除するプロファイル:", names, 0, False)
        if ok and name:
            self.settings.output_profiles.pop(name, None)
            save_settings(self.settings)
            self._refresh_profiles()

    # ---- 診断レポート ----
    def build_diagnostics(self) -> str:
        import platform
        import re
        from ..common import data_dir

        def plain(h: str) -> str:
            return html.unescape(re.sub(r"<[^>]+>", " ", (h or "").replace("<br>", "\n"))).strip()

        lines = [f"Miyamizu {__version__} / Python {platform.python_version()} / {platform.platform()}"]
        lines.append(f"engine: state={self.engine_state} pid={self.engine_hello.get('pid')} libsoxr={self.engine_hello.get('libsoxr')}")
        hello = self.engine_hello
        if hello.get("cpu"):
            lines.append(f"cpu: {hello['cpu'].get('topology')} / {hello['cpu'].get('freq')}")
        lines.append("devices: " + ", ".join(f"{d.get('name')}({d.get('label')})" for d in hello.get("devices", [])))
        d = self.settings.to_dict()
        d.pop("output_profiles", None)
        d.pop("library_dir", None)
        lines.append("settings: " + json.dumps(d, ensure_ascii=False))
        lines.append("profile: " + str(self._current_profile_name()))
        lines.append("--- signal path ---\n" + plain(self.lbl_signal.text()))
        lines.append("--- metrics ---\n" + plain(self.lbl_metrics.text()))
        try:
            tail = (data_dir() / "engine.log").read_text(errors="replace").splitlines()[-40:]
            lines.append("--- engine.log (tail) ---\n" + "\n".join(tail))
        except OSError:
            pass
        return "\n".join(lines)

    def copy_diagnostics(self):
        QApplication.clipboard().setText(self.build_diagnostics())
        self.statusBar().showMessage("診断レポートをクリップボードにコピーしました", 5000)

    def show_about(self):
        from ..common import app_icon

        box = QMessageBox(self)
        box.setWindowTitle("Miyamizu")
        box.setText(f"<b>Miyamizu {__version__}</b><br>時間軸の正確さを優先する音楽プレイヤー"
                    "<br><br><small>Sample rate converter designed by Aleksey Vaneev of Voxengo<br>"
                    "r8brain-free-src (MIT License)</small>")
        box.setIconPixmap(app_icon().pixmap(QSize(96, 96)))      # メッセージボックスの大きさはほぼそのまま
        box.exec()

    def open_capture_dialog(self):
        dlg = CaptureDialog(self)
        self._cap_dlg = dlg
        dlg.refresh_requested.connect(lambda: self.client.send("list_streams"))
        dlg.chosen.connect(lambda idx, label: (self.client.send("capture_start", sink_input=idx, label=label), dlg.accept()))
        self.client.send("list_streams")
        dlg.exec()
        self._cap_dlg = None

    def open_abx_dialog(self):
        names = list(self.settings.output_profiles)
        if len(names) < 2:
            QMessageBox.information(self, "ABX 聴き比べ", "比べる出力プロファイルが2つ以上必要です。\n"
                                    "「出力プロファイル → 現在の出力設定をプロファイルとして保存…」で、比べたい設定を保存してください。")
            return
        if self.cur is None or self.engine_state not in ("playing", "paused"):
            QMessageBox.information(self, "ABX 聴き比べ", "先に曲を再生してから開いてください(同じ位置で切り替えて聴き比べます)。")
            return
        if getattr(self, "_abx", None) is not None:
            self._abx.raise_()
            return
        d = ABXDialog(names, self)
        d.switch_requested.connect(lambda n: self.apply_profile(n, silent=True))
        d.closed.connect(self._abx_closed)
        self._abx = d
        self._refresh_profiles()
        d.show()

    def _abx_closed(self):
        self._abx = None
        self._refresh_profiles()

    def release_device(self):
        """再生を止め、排他していた出力デバイスを PipeWire/OS に返す。"""
        self.client.send("release_device")

    def on_disconnected(self):
        self.statusBar().showMessage("⚠ エンジンとの接続が切れました。再生ボタンで再接続します", 0)
        self.engine_state = "stopped"
        QTimer.singleShot(500, self._reconnect)

    def _reconnect(self):
        if self.client.start(timeout_s=6.0):
            self.client.send("hello")
            self.statusBar().showMessage("エンジンに再接続しました", 4000)

    def _on_status(self, ev: dict):
        self.engine_state = ev["state"]
        icon = QStyle.StandardPixmap.SP_MediaPause if ev["state"] == "playing" else QStyle.StandardPixmap.SP_MediaPlay
        self.btn_play.setIcon(self.style().standardIcon(icon))
        self.cur_dur = ev.get("dur", 0.0)
        self._pos = ev.get("pos", 0.0)
        self.lbl_pos.setText(fmt_time(self._pos))
        self.lbl_dur.setText(fmt_time(self.cur_dur))
        if not self.seeking and self.cur_dur > 0:
            self.seek.blockSignals(True)
            self.seek.setValue(int(1000 * min(1.0, self._pos / self.cur_dur)))
            self.seek.blockSignals(False)
        if ev["state"] == "stopped" and self.cur is None:
            self.lbl_title.setText("停止中")

    def _on_track_changed(self, ev: dict):
        t = self.known.get(ev.get("id"))
        if t is None:
            t = ev.get("track") or {}
        if t and self.order.cur is not None and self.order.cur.get("id") != t.get("id") and t.get("id") in self.known:
            self.order.on_started(self.known[t["id"]])
        if t.get("id") in self.known:
            self.cur = self.known[t["id"]]
        self._mark_current(self.cur)
        self._refresh_queue()
        title = (self.cur or t).get("title") or ""
        artist = (self.cur or t).get("artist") or ""
        self.lbl_title.setText(f"{title}" + (f"  —  {artist}" if artist else ""))
        self.lbl_path.setText((self.cur or t).get("album") or "")
        self.setWindowTitle(f"{title} - Miyamizu" if title else "Miyamizu")
        self._show_signal(ev.get("signal") or {})
        self._load_cover((self.cur or t).get("path", ""))

    def _show_signal(self, s: dict):
        if not s:
            return
        steps = "".join(f"<li>{x}</li>" for x in s.get("steps", []))
        be = s.get("backend")
        way = ({"alsa_hw": "排他出力(WASAPI 排他)", "pipewire": "共有出力(WASAPI 共有)", "custom": "共有出力(WASAPI)"} if IS_WIN else
               {"alsa_hw": "排他出力(ALSA hw 直結)", "custom": "ALSAデバイス直接", "pipewire": "共有出力(PipeWire)"}).get(be, str(be))
        badge = (f"<span style='color:#58c4a0'>✔ ビットパーフェクト</span> / {way}" if s.get("bitperfect")
                 else f"<span style='color:#c9a85a'>加工あり</span> / {way}")
        notes = "".join(f"<li style='color:#c9a85a'>{n}</li>" for n in s.get("notes", []))
        ram = " ・ RAM再生" if s.get("ram") else ""
        self.lbl_signal.setText(
            f"<b>ソース</b> {s.get('source')}<br>"
            + (f"<b>処理</b><ul style='margin:2px 0 2px -18px'>{steps}</ul>" if steps else "<b>処理</b> なし(素通し)<br>")
            + f"<b>出力</b> {s.get('output')}<br><b>デバイス</b> {s.get('device')} <i>({s.get('backend')})</i>{ram}<br>{badge}"
            + (f"<ul style='margin:2px 0 2px -18px'>{notes}</ul>" if notes else ""))

    def _on_metrics(self, ev: dict):
        # 静音モード: 最小化中・メトリクスが見えていない間は描画しない(GUI 側の再描画負荷を減らす)
        if self.settings.quiet_mode and self.engine_state == "playing" and (self.isMinimized() or not self.dock_metrics.isVisible()):
            return
        o = ev["out"]
        self.hist.set_data(o["hist"], o["hist_edges_us"])
        inv = o["ctxt_involuntary"]
        d_inv = inv - self._last_inv_cs if self._last_inv_cs else 0
        self._last_inv_cs = inv
        fill = ""
        if o.get("fill"):
            a, c, ring = o["fill"]
            fill = f"<br>バッファ残量: {a}/{c} フレーム ({'ストリーム' if ring else 'RAM'})"
        self.lbl_metrics.setText(
            f"RT: {html.escape(str(o['rt']))}<br>affinity: {o['affinity']}<br>"
            f"周期ずれ 平均 {o['dev_mean_us']:.1f} µs / 最大 {o['dev_max_us']:.0f} µs ({o['periods']} 周期)<br>"
            f"アンダーラン {o['underruns']} / xrun {o['xruns']} / 非自発CS {inv} (+{d_inv}/s)<br>"
            f"ピリオド {o['period_frames']}f / バッファ {o['buffer_frames']}f / 遅延 {o['delay_frames']}f" + fill
            + (f"<br>アプリ音声 遅延 約 {ev['cap_latency_ms']:.0f} ms(Miyamizu 内。PipeWire 側で別に約 20〜40 ms)" if ev.get("cap_latency_ms") is not None else "")
            + self._ram_summary(ev.get("ram") or []))
        self._on_ram(ev.get("ram") or [])
        self.freqw.set_data(ev.get("freq", {}), ev.get("ded", []), ev.get("reserved", []))

    def _load_cover(self, path: str):
        if not path or path.startswith(("http://", "https://")):
            return
        key = str(Path(path).parent) + "|" + (self.cur.get("album") or "" if self.cur else "")
        if key == self.cover_key:
            return
        self.cover_key = key
        if key in self.cover_cache:
            self.bg.set_image(self.cover_cache[key])
            return
        w = CoverWorker(path, self)
        w.loaded.connect(lambda p, img, k=key: self._on_cover(k, img))
        self._retiring.append(w)
        w.finished.connect(lambda ww=w: self._retiring.remove(ww) if ww in self._retiring else None)
        w.start()

    def _on_cover(self, key: str, img: QImage):
        self.cover_cache[key] = img
        if len(self.cover_cache) > 24:
            self.cover_cache.pop(next(iter(self.cover_cache)))
        if key == self.cover_key:
            self.bg.set_image(img)

    # ---------------------------------------------------------------- 設定
    def _apply_gui_confine(self):
        s = self.settings
        ded, others = sysinfo.plan_cpus(s.dedicated_cpu, s.reserve_sibling)
        if s.confine_gui:
            sysinfo.confine_process(others)     # 専用なしなら全 CPU へ戻す

    def _toggle_quiet(self, on: bool):
        """静音モード: 再生中の状態通知・メトリクス・準備プロセスの待機を減らして、再生スレッドへの干渉を抑える。"""
        self.settings.quiet_mode = bool(on)
        save_settings(self.settings)
        self.client.send("configure", settings=self.settings.to_dict())
        self.statusBar().showMessage("静音モード: " + ("オン(再生位置・メトリクスの更新を間引きます)" if on else "オフ"), 5000)

    def open_settings(self):
        dlg = SettingsDialog(self.settings, self.engine_hello, self)
        dlg.probe_requested.connect(lambda dev: self.client.send("probe_device", device=dev))
        self._settings_dlg = dlg
        if dlg.exec():
            old_dir = self.settings.library_dir
            old_exts = active_exts(self.settings)
            vol, rep, shf = self.settings.volume, self.settings.repeat, self.settings.shuffle
            self.settings = dlg.result_settings()
            self.settings.volume, self.settings.repeat, self.settings.shuffle = vol, rep, shf
            save_settings(self.settings)
            self.client.send("configure", settings=self.settings.to_dict())
            self.vol.setEnabled(self.settings.backend != "alsa_hw")
            self.vol.setToolTip("ビットパーフェクト(ALSA hw)では音量は0dB固定です" if self.settings.backend == "alsa_hw" else "")
            self._apply_gui_confine()
            self.menu_debug.menuAction().setVisible(bool(self.settings.debug_enabled))
            if not self.settings.debug_enabled and getattr(self, "_abx", None) is not None:
                self._abx.close()
            exts_changed = active_exts(self.settings) != old_exts
            if self.settings.library_dir != old_dir and self.settings.library_dir:
                self._set_tree_root(self.settings.library_dir)
                self.show_dir(self.settings.library_dir)
                self.start_scan(self.settings.library_dir, reset=exts_changed)
            elif exts_changed and (self.settings.library_dir or self.view_dir):
                self.start_scan(self.settings.library_dir or self.view_dir, reset=True)
            else:
                self._reload_view()
            self._apply_ram_column()
            if self.cur is not None:
                self._send_next(self.cur)
            self._refresh_queue()
        self._settings_dlg = None

    # ---------------------------------------------------------------- 終了
    def closeEvent(self, ev):
        self.qs.setValue("geometry", self.saveGeometry())
        self.qs.setValue("state", self.saveState())
        self.qs.setValue("layout_ver", LAYOUT_VER)
        save_settings(self.settings)
        if self.scan_worker is not None and self.scan_worker.isRunning():
            self.scan_worker.cancel()
            self.scan_worker.wait(3000)
        for w in list(self._retiring):
            w.wait(1000)
        self.client.shutdown_engine()
        self.lib.close()
        super().closeEvent(ev)
