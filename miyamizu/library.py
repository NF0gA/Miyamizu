"""ライブラリ(SQLite)・CUE 展開・プレイリスト・M3U8・カバーアート。

- 差分スキャン: フォルダ内ファイルの (名前, mtime, サイズ) の署名が同じならそのフォルダは再読込しない
- CUE: 外部 .cue / FLAC 埋め込み CUE を「開始・終了フレーム付きの仮想トラック」に展開。元ファイルは is_cue_source=1 で既定非表示
- プレイリストはパス+開始フレームで曲を参照。ライブラリに無い曲は「利用できません」として残す
"""
from __future__ import annotations

import hashlib
import os
import re
import sqlite3
import threading
from pathlib import Path
from typing import Callable, Iterable

from .common import AUDIO_EXTS, data_dir
from .cue import (CueSheet, frames_to_samples, parse_cue_file, read_embedded_cue, resolve_audio_file)

SCHEMA = """
CREATE TABLE IF NOT EXISTS tracks(
  id INTEGER PRIMARY KEY,
  path TEXT NOT NULL,
  folder TEXT NOT NULL,
  start_frame INTEGER NOT NULL DEFAULT 0,
  end_frame INTEGER,
  end_nopregap INTEGER,
  cue_path TEXT,
  group_key TEXT,
  is_cue_source INTEGER NOT NULL DEFAULT 0,
  disc INTEGER, track_no INTEGER,
  title TEXT, artist TEXT, album TEXT, albumartist TEXT, date TEXT, genre TEXT,
  rate INTEGER, channels INTEGER, bits INTEGER, codec TEXT, duration REAL,
  UNIQUE(path, start_frame)
);
CREATE INDEX IF NOT EXISTS idx_tracks_folder ON tracks(folder);
CREATE TABLE IF NOT EXISTS dirs(path TEXT PRIMARY KEY, sig TEXT);
CREATE TABLE IF NOT EXISTS playlists(id INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE);
CREATE TABLE IF NOT EXISTS playlist_items(
  id INTEGER PRIMARY KEY, playlist_id INTEGER NOT NULL, pos INTEGER NOT NULL,
  path TEXT NOT NULL, start_frame INTEGER NOT NULL DEFAULT 0, title TEXT, end_frame INTEGER, duration REAL
);
CREATE INDEX IF NOT EXISTS idx_pli ON playlist_items(playlist_id, pos);
"""

TRACK_COLS = ("id,path,folder,group_key,start_frame,end_frame,end_nopregap,cue_path,is_cue_source,disc,track_no,"
              "title,artist,album,albumartist,date,genre,rate,channels,bits,codec,duration")


def natural_key(s: str):
    return [int(t) if t.isdigit() else t.lower() for t in re.split(r"(\d+)", s)]


def _first(tags, key):
    v = tags.get(key) if tags else None
    if not v:
        return ""
    return str(v[0]) if isinstance(v, (list, tuple)) else str(v)


def _num(s: str) -> int | None:
    m = re.match(r"\s*(\d+)", s or "")
    return int(m.group(1)) if m else None


def read_info(path: str) -> dict | None:
    """タグと基本情報。読めないファイルは None。"""
    try:
        import mutagen

        mf = mutagen.File(path, easy=True)
    except Exception:
        mf = None
    info = getattr(mf, "info", None)
    tags = getattr(mf, "tags", None)
    d = {"title": "", "artist": "", "album": "", "albumartist": "", "date": "", "genre": "",
         "track_no": None, "disc": None, "rate": 0, "channels": 0, "bits": 0, "length": 0.0, "frames": 0,
         "codec": Path(path).suffix.lstrip(".").upper()}
    if tags:
        d["title"] = _first(tags, "title")
        d["artist"] = _first(tags, "artist")
        d["album"] = _first(tags, "album")
        d["albumartist"] = _first(tags, "albumartist")
        d["date"] = _first(tags, "date")
        d["genre"] = _first(tags, "genre")
        d["track_no"] = _num(_first(tags, "tracknumber"))
        d["disc"] = _num(_first(tags, "discnumber"))
    if info is not None:
        d["rate"] = int(getattr(info, "sample_rate", 0) or 0)
        d["channels"] = int(getattr(info, "channels", 0) or 0)
        d["bits"] = int(getattr(info, "bits_per_sample", 0) or 0)
        d["length"] = float(getattr(info, "length", 0.0) or 0.0)
        codec = str(getattr(info, "codec", "") or "").lower()      # MP4: alac / mp4a.40.2 など
        if codec.startswith("alac"):
            d["codec"] = "ALAC"
        elif codec.startswith("mp4a"):
            d["codec"] = "AAC"
        ts = getattr(info, "total_samples", None)
        d["frames"] = int(ts) if ts else int(round(d["length"] * d["rate"]))
    if not d["rate"] or not d["frames"]:
        try:   # mutagen で取れない形式のフォールバック
            from .decode import probe

            p = probe(path)
            d["rate"], d["channels"], d["frames"] = p["rate"], p["channels"], p["frames"]
            d["length"] = p["frames"] / p["rate"] if p["rate"] else 0.0
            d["bits"] = d["bits"] or (p["src_bits"] if p["is_int"] else 0)
        except Exception:
            return None
    return d


class Library:
    """スレッドごとに別インスタンスを作って使う(SQLite 接続はスレッド間共有しない)。"""

    def __init__(self, db_path: str | Path | None = None):
        self.db_path = str(db_path or (data_dir() / "library.sqlite"))
        self.db = sqlite3.connect(self.db_path, timeout=30)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=NORMAL")
        self.db.executescript(SCHEMA)
        cols = {r["name"] for r in self.db.execute("PRAGMA table_info(tracks)").fetchall()}
        if "group_key" not in cols:       # 旧版 DB からの移行: 列を足して全フォルダを再スキャン対象にする
            self.db.execute("ALTER TABLE tracks ADD COLUMN group_key TEXT")
        if self.db.execute("PRAGMA user_version").fetchone()[0] < 2:     # v2: CUE フォルダ化 + ALAC 表記
            self.db.execute("UPDATE dirs SET sig=''")
            self.db.execute("PRAGMA user_version=2")
        self.db.execute("CREATE INDEX IF NOT EXISTS idx_tracks_group ON tracks(group_key)")
        self.db.commit()

    def close(self):
        try:
            self.db.close()
        except sqlite3.Error:
            pass

    # ------------------------------------------------------------ スキャン
    @staticmethod
    def _dir_signature(files: list[Path]) -> str:
        h = hashlib.sha1()
        for f in sorted(files, key=lambda p: p.name):
            try:
                st = f.stat()
            except OSError:
                continue
            h.update(f"{f.name}|{st.st_mtime_ns}|{st.st_size}\n".encode())
        return h.hexdigest()

    def scan(self, root: str, progress: Callable[[str, int], None] | None = None,
             cancel: Callable[[], bool] | None = None, exts: set[str] | None = None) -> int:
        """root 以下を差分スキャン。更新したフォルダ数を返す。exts: 取り込む拡張子(省略時は AUDIO_EXTS 全部)。"""
        exts = AUDIO_EXTS if exts is None else exts
        changed = 0
        seen_dirs: list[str] = []
        n_files = 0
        for dirpath, dirnames, filenames in os.walk(root):
            if cancel and cancel():
                break
            dirnames[:] = sorted((d for d in dirnames if not d.startswith(".")), key=natural_key)
            dp = Path(dirpath)
            audio = [dp / f for f in filenames if Path(f).suffix.lower() in exts]
            cues = [dp / f for f in filenames if f.lower().endswith(".cue")]
            seen_dirs.append(dirpath)
            n_files += len(audio)
            sig = self._dir_signature(audio + cues)
            row = self.db.execute("SELECT sig FROM dirs WHERE path=?", (dirpath,)).fetchone()
            if row and row["sig"] == sig:
                continue
            self._rescan_dir(dp, audio, cues)
            self.db.execute("INSERT OR REPLACE INTO dirs(path,sig) VALUES(?,?)", (dirpath, sig))
            self.db.commit()
            changed += 1
            if progress:
                progress(dirpath, n_files)
        if not (cancel and cancel()):
            self._prune(root, set(seen_dirs))
        return changed

    def reset_dir_cache(self):
        """拡張子設定が変わった時など、全フォルダを再スキャン対象にする。"""
        self.db.execute("DELETE FROM dirs")
        self.db.commit()

    def _prune(self, root: str, seen: set[str]):
        root_prefix = root.rstrip("/") + "/"
        rows = self.db.execute("SELECT path FROM dirs").fetchall()
        for r in rows:
            p = r["path"]
            if (p == root or p.startswith(root_prefix)) and p not in seen:
                self.db.execute("DELETE FROM tracks WHERE folder=?", (p,))
                self.db.execute("DELETE FROM dirs WHERE path=?", (p,))
        self.db.commit()

    def _rescan_dir(self, dp: Path, audio: list[Path], cues: list[Path]):
        folder = str(dp)
        self.db.execute("DELETE FROM tracks WHERE folder=?", (folder,))
        claimed: set[str] = set()
        # 1) 外部 CUE
        for cp in sorted(cues, key=lambda p: natural_key(p.name)):
            try:
                sheet = parse_cue_file(cp)
            except OSError:
                continue
            claimed |= self._insert_cue(sheet, cp, dp)
        # 2) 普通のファイル / FLAC 埋め込み CUE
        for ap in sorted(audio, key=lambda p: natural_key(p.name)):
            if str(ap) in claimed:
                continue
            info = read_info(str(ap))
            if info is None:
                continue
            emb = read_embedded_cue(ap) if ap.suffix.lower() == ".flac" else None
            if emb and emb.files and len(emb.files[0].tracks) > 1:
                self._insert_cue(emb, None, dp, embedded=ap)
                continue
            self.db.execute(
                "INSERT OR REPLACE INTO tracks(path,folder,group_key,start_frame,end_frame,end_nopregap,is_cue_source,disc,track_no,"
                "title,artist,album,albumartist,date,genre,rate,channels,bits,codec,duration)"
                " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (str(ap), folder, folder, 0, None, None, 0, info["disc"], info["track_no"],
                 info["title"] or ap.stem, info["artist"], info["album"], info["albumartist"], info["date"],
                 info["genre"], info["rate"], info["channels"], info["bits"], info["codec"], info["length"]))

    def _insert_cue(self, sheet: CueSheet, cue_path: Path | None, dp: Path, embedded: Path | None = None) -> set[str]:
        claimed: set[str] = set()
        # CUE(または埋め込み CUE のファイル)は「1つのフォルダ」として扱う: 同じフォルダに複数あってもアルバムごとに分かれる
        gkey = str(cue_path) if cue_path else str(embedded)
        for cf in sheet.files:
            ap = embedded or resolve_audio_file(dp, cf.name)
            if ap is None or not cf.tracks:
                continue
            info = read_info(str(ap))
            if info is None or not info["rate"]:
                continue
            claimed.add(str(ap))
            rate, total = info["rate"], info["frames"]
            # 元ファイル自体は非表示行として保持(再生はしない)
            self.db.execute(
                "INSERT OR REPLACE INTO tracks(path,folder,group_key,start_frame,end_frame,is_cue_source,title,artist,album,rate,"
                "channels,bits,codec,duration) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (str(ap), str(dp), gkey, -1, None, 1, ap.stem, sheet.performer, sheet.album, rate, info["channels"],
                 info["bits"], info["codec"], info["length"]))
            n = len(cf.tracks)
            for i, t in enumerate(cf.tracks):
                start = frames_to_samples(t.index01, rate)
                if i + 1 < n:
                    nxt = cf.tracks[i + 1]
                    end = frames_to_samples(nxt.index01, rate)
                    end_np = frames_to_samples(nxt.index00, rate) if nxt.index00 is not None else end
                else:
                    end = end_np = total
                if end <= start:
                    continue
                self.db.execute(
                    "INSERT OR REPLACE INTO tracks(path,folder,group_key,start_frame,end_frame,end_nopregap,cue_path,"
                    "is_cue_source,disc,track_no,title,artist,album,albumartist,date,genre,rate,channels,bits,codec,duration)"
                    " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (str(ap), str(dp), gkey, start, end, end_np, str(cue_path) if cue_path else None, 0, None, t.number,
                     t.title or f"Track {t.number:02d}", t.performer or sheet.performer, sheet.album,
                     sheet.performer, sheet.date, sheet.genre, rate, info["channels"], info["bits"], info["codec"],
                     (end - start) / rate))
        return claimed

    # ------------------------------------------------------------ 取得
    @staticmethod
    def _order(rows: Iterable[sqlite3.Row]) -> list[dict]:
        out = [dict(r) for r in rows]
        out.sort(key=lambda t: (natural_key(t["folder"]), natural_key(t.get("group_key") or t["folder"]), t["disc"] or 0,
                                t["track_no"] if t["track_no"] is not None else 10_000,
                                natural_key(Path(t["path"]).name), t["start_frame"]))
        return out

    def tracks_in_dir(self, folder: str, recursive: bool = True, show_cue_source: bool = False) -> list[dict]:
        folder = folder.rstrip("/") or "/"
        cond = "" if show_cue_source else " AND is_cue_source=0"
        if recursive:
            like = folder.rstrip("/") + "/%"
            rows = self.db.execute(f"SELECT {TRACK_COLS} FROM tracks WHERE (folder=? OR folder LIKE ?){cond}",
                                   (folder, like)).fetchall()
        else:
            rows = self.db.execute(f"SELECT {TRACK_COLS} FROM tracks WHERE folder=?{cond}", (folder,)).fetchall()
        return self._order(rows)

    def tracks_in_group(self, key: str) -> list[dict]:
        """通常フォルダ(そのフォルダ直下のCUE以外の曲)、または CUE 1枚(=1アルバム)の曲。"""
        rows = self.db.execute(f"SELECT {TRACK_COLS} FROM tracks WHERE group_key=? AND is_cue_source=0", (key,)).fetchall()
        return self._order(rows)

    def cue_groups(self, folder: str) -> list[dict]:
        """フォルダ直下にある CUE アルバム(ツリーに子ノードとして出す)。"""
        rows = self.db.execute(
            "SELECT group_key, MAX(album) AS album, MAX(artist) AS artist, COUNT(*) AS n FROM tracks "
            "WHERE folder=? AND group_key<>folder AND is_cue_source=0 GROUP BY group_key", (folder,)).fetchall()
        out = [dict(r) for r in rows]
        out.sort(key=lambda g: natural_key(g["group_key"]))
        return out

    def all_tracks(self, show_cue_source: bool = False) -> list[dict]:
        cond = "" if show_cue_source else " WHERE is_cue_source=0"
        return self._order(self.db.execute(f"SELECT {TRACK_COLS} FROM tracks{cond}").fetchall())

    def track_by_id(self, tid: int) -> dict | None:
        r = self.db.execute(f"SELECT {TRACK_COLS} FROM tracks WHERE id=?", (tid,)).fetchone()
        return dict(r) if r else None

    def track_by_path(self, path: str, start_frame: int = 0) -> dict | None:
        r = self.db.execute(f"SELECT {TRACK_COLS} FROM tracks WHERE path=? AND start_frame=?",
                            (path, start_frame)).fetchone()
        return dict(r) if r else None

    def count(self) -> int:
        return int(self.db.execute("SELECT COUNT(*) FROM tracks WHERE is_cue_source=0").fetchone()[0])

    # ------------------------------------------------------------ プレイリスト
    def playlists(self) -> list[dict]:
        rows = self.db.execute(
            "SELECT p.id, p.name, (SELECT COUNT(*) FROM playlist_items i WHERE i.playlist_id=p.id) AS n "
            "FROM playlists p ORDER BY p.name COLLATE NOCASE").fetchall()
        return [dict(r) for r in rows]

    def create_playlist(self, name: str) -> int:
        name = name.strip() or "新しいプレイリスト"
        base, k = name, 2
        while self.db.execute("SELECT 1 FROM playlists WHERE name=?", (name,)).fetchone():
            name = f"{base} ({k})"
            k += 1
        cur = self.db.execute("INSERT INTO playlists(name) VALUES(?)", (name,))
        self.db.commit()
        return int(cur.lastrowid)

    def rename_playlist(self, pid: int, name: str):
        try:
            self.db.execute("UPDATE playlists SET name=? WHERE id=?", (name.strip(), pid))
            self.db.commit()
        except sqlite3.IntegrityError:
            pass

    def delete_playlist(self, pid: int):
        self.db.execute("DELETE FROM playlist_items WHERE playlist_id=?", (pid,))
        self.db.execute("DELETE FROM playlists WHERE id=?", (pid,))
        self.db.commit()

    def add_to_playlist(self, pid: int, tracks: list[dict]) -> int:
        pos = self.db.execute("SELECT COALESCE(MAX(pos),-1) FROM playlist_items WHERE playlist_id=?", (pid,)).fetchone()[0]
        for t in tracks:
            pos += 1
            self.db.execute(
                "INSERT INTO playlist_items(playlist_id,pos,path,start_frame,title,end_frame,duration) VALUES(?,?,?,?,?,?,?)",
                (pid, pos, t["path"], t.get("start_frame", 0) if t.get("start_frame", 0) >= 0 else 0,
                 f"{t.get('artist') or ''} - {t.get('title') or ''}".strip(" -"), t.get("end_frame"),
                 t.get("duration")))
        self.db.commit()
        return len(tracks)

    def remove_playlist_items(self, item_ids: list[int]):
        self.db.executemany("DELETE FROM playlist_items WHERE id=?", [(i,) for i in item_ids])
        self.db.commit()

    def move_playlist_item(self, pid: int, item_id: int, new_index: int):
        ids = [r["id"] for r in self.db.execute(
            "SELECT id FROM playlist_items WHERE playlist_id=? ORDER BY pos", (pid,)).fetchall()]
        if item_id not in ids:
            return
        ids.remove(item_id)
        ids.insert(max(0, min(new_index, len(ids))), item_id)
        for pos, i in enumerate(ids):
            self.db.execute("UPDATE playlist_items SET pos=? WHERE id=?", (pos, i))
        self.db.commit()

    def playlist_tracks(self, pid: int) -> list[dict]:
        """曲リスト。ライブラリに無い曲は missing=True の行として返す。"""
        out: list[dict] = []
        for r in self.db.execute("SELECT * FROM playlist_items WHERE playlist_id=? ORDER BY pos", (pid,)).fetchall():
            t = self.track_by_path(r["path"], r["start_frame"])
            if t is None and r["start_frame"] == 0:
                t = self.track_by_path(r["path"], 0)
            if t is not None:
                t["item_id"] = r["id"]
                t["missing"] = not os.path.exists(t["path"])
            else:
                exists = os.path.exists(r["path"])
                t = {"id": None, "path": r["path"], "start_frame": r["start_frame"], "end_frame": r["end_frame"],
                     "title": r["title"] or Path(r["path"]).stem, "artist": "", "album": "", "duration": r["duration"],
                     "item_id": r["id"], "missing": not exists, "folder": str(Path(r["path"]).parent),
                     "end_nopregap": r["end_frame"], "rate": 0, "channels": 0, "bits": 0, "codec": "",
                     "disc": None, "track_no": None, "albumartist": "", "date": "", "genre": "",
                     "cue_path": None, "is_cue_source": 0, "group_key": str(Path(r["path"]).parent)}
            out.append(t)
        return out

    # ------------------------------------------------------------ M3U8
    def export_m3u8(self, pid: int, path: str) -> int:
        tracks = self.playlist_tracks(pid)
        lines = ["#EXTM3U"]
        for t in tracks:
            dur = int(t.get("duration") or 0)
            lines.append(f"#EXTINF:{dur},{(t.get('artist') or '')} - {t.get('title') or ''}".rstrip(" -"))
            if t.get("start_frame", 0) > 0 or t.get("end_frame"):
                lines.append(f"#MIYAMIZU:start={t.get('start_frame', 0)};end={t.get('end_frame') or ''}")
            lines.append(t["path"])
        Path(path).write_text("\n".join(lines) + "\n", encoding="utf-8")
        return len(tracks)

    def import_m3u8(self, path: str, name: str | None = None) -> int:
        from .cue import decode_text

        text = decode_text(Path(path).read_bytes())
        base = Path(path).parent
        pid = self.create_playlist(name or Path(path).stem)
        title, start, end = "", 0, None
        n = 0
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            if line.startswith("#EXTINF:"):
                title = line.split(",", 1)[1] if "," in line else ""
            elif line.startswith(("#MIYAMIZU:", "#HIFIPLAYER:")):      # 旧名の M3U8 も読む
                m = dict(kv.split("=", 1) for kv in line.split(":", 1)[1].split(";") if "=" in kv)
                start = int(m.get("start") or 0)
                end = int(m["end"]) if m.get("end") else None
            elif line.startswith("#"):
                continue
            else:
                p = Path(line) if os.path.isabs(line) else base / line
                self.add_to_playlist(pid, [{"path": str(p), "start_frame": start, "end_frame": end, "title": title,
                                            "artist": "", "duration": None}])
                title, start, end = "", 0, None
                n += 1
        return n


# ================================================================ カバーアート
_COVER_NAMES = ("cover", "folder", "front", "album", "albumart", "artwork", "jacket")
_IMG_EXTS = (".jpg", ".jpeg", ".png", ".webp", ".bmp")


def extract_cover(path: str) -> bytes | None:
    """埋め込みアート → フォルダ内の画像の順で探す。"""
    try:
        import mutagen
        from mutagen.flac import FLAC
        from mutagen.id3 import ID3
        from mutagen.mp4 import MP4

        ext = Path(path).suffix.lower()
        if ext == ".flac":
            pics = FLAC(path).pictures
            if pics:
                best = next((p for p in pics if p.type == 3), pics[0])
                return best.data
        elif ext == ".mp3":
            for k, v in ID3(path).items():
                if k.startswith("APIC"):
                    return v.data
        elif ext in (".m4a", ".aac", ".alac"):
            mp = MP4(path)
            covr = mp.tags.get("covr") if mp.tags else None
            if covr:
                return bytes(covr[0])
        else:
            mf = mutagen.File(path)
            for p in getattr(mf, "pictures", []) or []:
                return p.data
    except Exception:
        pass
    d = Path(path).parent
    try:
        files = [f for f in d.iterdir() if f.suffix.lower() in _IMG_EXTS]
    except OSError:
        return None
    files.sort(key=lambda f: (0 if f.stem.lower() in _COVER_NAMES else 1, f.name.lower()))
    for f in files:
        try:
            return f.read_bytes()
        except OSError:
            continue
    return None
