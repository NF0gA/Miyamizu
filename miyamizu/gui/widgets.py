"""GUI 部品: 背景アート、曲モデル、ジッターヒストグラム、CPU 周波数バー。"""
from __future__ import annotations

import json

from PyQt6.QtCore import QAbstractTableModel, QMimeData, QModelIndex, QRectF, Qt, QPointF
from PyQt6.QtGui import QBrush, QColor, QFont, QImage, QPainter, QPen, QPixmap
from PyQt6.QtWidgets import QWidget

from ..common import fmt_time, rate_label

MIME_TRACKS = "application/x-miyamizu-tracks"
ACCENT = QColor(88, 196, 160)


class ArtBackground(QWidget):
    """アルバムアートを背景に敷く(アスペクト維持で塗りつぶし+暗くして可読性を確保)。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._img: QImage | None = None
        self._pix: QPixmap | None = None
        self._dim = 205          # 0-255 暗さ
        self.setAutoFillBackground(False)

    def set_image(self, img: QImage | None):
        self._img = img if (img is not None and not img.isNull()) else None
        self._pix = None
        self.update()

    def set_dim(self, v: int):
        self._dim = max(0, min(255, v))
        self.update()

    def paintEvent(self, ev):
        p = QPainter(self)
        p.fillRect(self.rect(), QColor(24, 26, 30))
        if self._img is not None:
            if self._pix is None or self._pix.size() != self.size():
                scaled = self._img.scaled(self.size(), Qt.AspectRatioMode.KeepAspectRatioByExpanding,
                                          Qt.TransformationMode.SmoothTransformation)
                x = (scaled.width() - self.width()) // 2
                y = (scaled.height() - self.height()) // 2
                self._pix = QPixmap.fromImage(scaled.copy(x, y, self.width(), self.height()))
            p.drawPixmap(0, 0, self._pix)
            p.fillRect(self.rect(), QColor(14, 16, 20, self._dim))
        p.end()


COLUMNS = ["", "RAM", "#", "タイトル", "アーティスト", "アルバム", "時間", "形式", "ファイル"]
COL_RAM, COL_NO, COL_TITLE, COL_ARTIST, COL_ALBUM, COL_DUR, COL_FMT, COL_FILE = range(1, 9)
RAM_COLOR = QColor(110, 190, 255)


class TrackModel(QAbstractTableModel):
    def __init__(self, parent=None, playlist: bool = False, queue: bool = False):
        super().__init__(parent)
        self.tracks: list[dict] = []
        self.playlist = playlist
        self.queue = queue                  # キュー表示: 先頭行=再生中、# は先頭からの順番
        self.cur_key: str | int | None = None       # 再生中の曲(エンジンの id)
        self.cur_track_id: int | None = None
        self.ram_by_key: dict = {}          # エンジンの曲 id → {"done", "pct", "mb", "role"}
        self.ram_by_track: dict = {}        # ライブラリの曲 id → 同上
        self._bold = QFont()
        self._bold.setBold(True)

    def set_tracks(self, tracks: list[dict]):
        self.beginResetModel()
        self.tracks = tracks
        self.endResetModel()

    def set_current(self, key, track_id):
        self.cur_key, self.cur_track_id = key, track_id
        if self.tracks:
            self.dataChanged.emit(self.index(0, 0), self.index(len(self.tracks) - 1, len(COLUMNS) - 1))

    def set_ram(self, by_key: dict, by_track: dict):
        if by_key == self.ram_by_key and by_track == self.ram_by_track:
            return
        self.ram_by_key, self.ram_by_track = by_key, by_track
        if self.tracks:
            self.dataChanged.emit(self.index(0, COL_RAM), self.index(len(self.tracks) - 1, COL_RAM))

    def ram_info(self, t: dict):
        if self.playlist:
            return self.ram_by_key.get(f"p{t.get('item_id')}")
        i = t.get("id")
        return self.ram_by_key.get(i) or self.ram_by_track.get(i) if i is not None else None

    def is_current(self, t: dict) -> bool:
        if self.cur_key is None:
            return False
        if self.playlist:
            return f"p{t.get('item_id')}" == self.cur_key
        return t.get("id") is not None and (t.get("id") == self.cur_key or t.get("id") == self.cur_track_id)

    def rowCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else len(self.tracks)

    def columnCount(self, parent=QModelIndex()):
        return len(COLUMNS)

    def headerData(self, section, orientation, role=Qt.ItemDataRole.DisplayRole):
        if orientation == Qt.Orientation.Horizontal and role == Qt.ItemDataRole.DisplayRole:
            return COLUMNS[section]
        return None

    def flags(self, index):
        f = Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable
        if index.isValid():
            f |= Qt.ItemFlag.ItemIsDragEnabled
        return f

    def mimeTypes(self):
        return [MIME_TRACKS]

    def mimeData(self, indexes):
        rows = sorted({i.row() for i in indexes})
        md = QMimeData()
        payload = [{"id": self.tracks[r].get("id"), "path": self.tracks[r]["path"],
                    "start_frame": self.tracks[r].get("start_frame", 0)} for r in rows]
        md.setData(MIME_TRACKS, json.dumps(payload).encode())
        return md

    def data(self, index, role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid():
            return None
        t = self.tracks[index.row()]
        c = index.column()
        is_cur = (index.row() == 0) if self.queue else self.is_current(t)
        if role == Qt.ItemDataRole.DisplayRole:
            if c == 0:
                return "▶" if is_cur else ("⚠" if t.get("missing") else "")
            if c == COL_RAM:
                r = self.ram_info(t)
                if not r:
                    return ""
                return "● RAM" if r["done"] else f"◐ {int(r['pct'] * 100)}%"
            if c == COL_NO:
                if self.queue:
                    return index.row() or ""
                return index.row() + 1 if self.playlist else (t.get("track_no") or "")
            if c == COL_TITLE:
                return t.get("title") or ""
            if c == COL_ARTIST:
                return t.get("artist") or ""
            if c == COL_ALBUM:
                return t.get("album") or ""
            if c == COL_DUR:
                return fmt_time(t["duration"]) if t.get("duration") else ""
            if c == COL_FMT:
                r, b = t.get("rate") or 0, t.get("bits") or 0
                codec = (t.get("codec") or "").split("/")[0]
                return f"{codec} {rate_label(r)}" + (f"/{b}bit" if b else "") if r else codec
            if c == COL_FILE:
                p = t.get("path", "")
                return p.rsplit("/", 1)[-1]
        elif role == Qt.ItemDataRole.ForegroundRole:
            if c == COL_RAM and self.ram_info(t):
                return QBrush(RAM_COLOR)
            if t.get("missing"):
                return QBrush(QColor(140, 140, 140))
            if is_cur:
                return QBrush(ACCENT)
        elif role == Qt.ItemDataRole.FontRole:
            if is_cur:
                return self._bold
        elif role == Qt.ItemDataRole.TextAlignmentRole:
            if c in (COL_NO, COL_DUR):
                return int(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        elif role == Qt.ItemDataRole.ToolTipRole:
            if t.get("missing"):
                return "ファイルが見つかりません: " + t.get("path", "")
            if c == COL_RAM:
                r = self.ram_info(t)
                if r:
                    return (f"RAM上にデコード済み ({r['mb']} MB)" if r["done"] else f"RAMへ読み込み中 {int(r['pct'] * 100)}%") \
                        + {"cur": "(再生中)", "next": "(次曲の先読み)"}.get(r.get("role"), "(事前変換・先行)")
            if c == COL_FILE:
                return t.get("path")
        return None


class HistogramWidget(QWidget):
    """周期ずれ(出力スレッドの書き込み間隔の揺れ)の分布。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.hist: list[int] = []
        self.edges: list[int] = []
        self.setMinimumHeight(90)

    def set_data(self, hist, edges):
        self.hist, self.edges = hist, edges
        self.update()

    def paintEvent(self, ev):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        w, h = self.width(), self.height()
        p.fillRect(self.rect(), QColor(30, 33, 38))
        if not self.hist or sum(self.hist) == 0:
            p.setPen(QColor(150, 150, 150))
            p.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, "計測待ち")
            return
        total = sum(self.hist)
        n = len(self.hist)
        bw = (w - 16) / n
        top = max(self.hist)
        labels = [f"<{e}" for e in self.edges] + [f"≥{self.edges[-1]}"]
        f = QFont(p.font())
        f.setPointSizeF(max(7.0, f.pointSizeF() - 1.5))
        p.setFont(f)
        for i, v in enumerate(self.hist):
            bh = (h - 34) * (v / top) if top else 0
            x = 8 + i * bw
            col = ACCENT if i <= 3 else (QColor(230, 190, 80) if i <= 5 else QColor(230, 100, 90))
            p.fillRect(QRectF(x + 2, h - 18 - bh, bw - 4, bh), col)
            p.setPen(QColor(190, 190, 190))
            p.drawText(QRectF(x, h - 16, bw, 14), Qt.AlignmentFlag.AlignCenter, labels[i])
            if v:
                p.drawText(QRectF(x, h - 32 - bh, bw, 14), Qt.AlignmentFlag.AlignCenter, f"{100.0 * v / total:.0f}%")
        p.end()


class FreqWidget(QWidget):
    """CPU コアごとの周波数。専用コアを強調表示。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.freq: dict[int, float] = {}
        self.ded: set[int] = set()
        self.reserved: set[int] = set()
        self.setMinimumHeight(70)

    def set_data(self, freq: dict, ded, reserved=()):
        self.freq = {int(k): v for k, v in freq.items()}
        self.ded = set(ded)
        self.reserved = set(reserved)
        self.update()

    def paintEvent(self, ev):
        p = QPainter(self)
        w, h = self.width(), self.height()
        p.fillRect(self.rect(), QColor(30, 33, 38))
        if not self.freq:
            p.setPen(QColor(150, 150, 150))
            p.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, "周波数を取得できません")
            return
        cpus = sorted(self.freq)
        fmax = max(max(self.freq.values()), 1.0)
        n = len(cpus)
        bw = (w - 12) / n
        f = QFont(p.font())
        f.setPointSizeF(max(7.0, f.pointSizeF() - 2))
        p.setFont(f)
        for i, c in enumerate(cpus):
            v = self.freq[c]
            bh = (h - 30) * (v / fmax)
            x = 6 + i * bw
            col = (QColor(240, 170, 60) if c in self.ded
                   else QColor(150, 110, 70) if c in self.reserved else QColor(110, 150, 220))
            p.fillRect(QRectF(x + 1, h - 16 - bh, bw - 2, bh), col)
            p.setPen(QColor(190, 190, 190))
            p.drawText(QRectF(x - 2, h - 15, bw + 4, 13), Qt.AlignmentFlag.AlignCenter, str(c))
        p.setPen(QColor(200, 200, 200))
        p.drawText(QPointF(8, 12), f"最大 {fmax:.0f} MHz  (橙=専用 / 茶=SMT兄弟を空け)")
        p.end()
