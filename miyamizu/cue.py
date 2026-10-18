"""CUE シートの解析。

- 文字コード自動判定 (UTF-8 BOM / UTF-8 / charset-normalizer / cp932)
- CUE 内の FILE 名と実ファイルの拡張子が違う場合の補正 (.wav と書かれているが実体は .flac 等)
- 複数 FILE の CUE
- INDEX 00 (プリギャップ) と INDEX 01 を保持
- FLAC 埋め込み CUE (Vorbis comment の CUESHEET / CUESHEET ブロック) の読み込み
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from .common import AUDIO_EXTS

CD_FRAMES_PER_SEC = 75


@dataclass
class CueTrack:
    number: int
    title: str = ""
    performer: str = ""
    index00: int | None = None   # CD フレーム(1/75秒)
    index01: int = 0
    isrc: str = ""


@dataclass
class CueFile:
    name: str
    tracks: list[CueTrack] = field(default_factory=list)


@dataclass
class CueSheet:
    album: str = ""
    performer: str = ""
    date: str = ""
    genre: str = ""
    files: list[CueFile] = field(default_factory=list)


def decode_text(raw: bytes) -> str:
    if raw.startswith(b"\xef\xbb\xbf"):
        return raw[3:].decode("utf-8", errors="replace")
    if raw.startswith((b"\xff\xfe", b"\xfe\xff")):
        return raw.decode("utf-16", errors="replace")
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        pass
    try:
        from charset_normalizer import from_bytes

        best = from_bytes(raw).best()
        if best is not None and best.encoding:
            return str(best)
    except Exception:
        pass
    try:
        return raw.decode("cp932")
    except UnicodeDecodeError:
        return raw.decode("latin-1", errors="replace")


_TOKEN = re.compile(r'"([^"]*)"|(\S+)')


def _tokens(line: str) -> list[str]:
    return [m.group(1) if m.group(1) is not None else m.group(2) for m in _TOKEN.finditer(line)]


def _msf_to_frames(s: str) -> int:
    try:
        m, sec, f = (int(x) for x in s.split(":"))
    except ValueError:
        return 0
    return (m * 60 + sec) * CD_FRAMES_PER_SEC + f


def parse_cue_text(text: str) -> CueSheet:
    sheet = CueSheet()
    cur_file: CueFile | None = None
    cur_track: CueTrack | None = None
    for line in text.splitlines():
        line = line.strip().lstrip("﻿")
        if not line:
            continue
        tk = _tokens(line)
        if not tk:
            continue
        key = tk[0].upper()
        if key == "REM" and len(tk) >= 3:
            rk = tk[1].upper()
            if rk == "DATE":
                sheet.date = tk[2]
            elif rk == "GENRE":
                sheet.genre = " ".join(tk[2:])
        elif key == "FILE" and len(tk) >= 2:
            cur_file = CueFile(name=tk[1])
            sheet.files.append(cur_file)
            cur_track = None
        elif key == "TRACK" and len(tk) >= 2 and cur_file is not None:
            try:
                n = int(tk[1])
            except ValueError:
                n = len(cur_file.tracks) + 1
            cur_track = CueTrack(number=n)
            if len(tk) >= 3 and tk[2].upper() != "AUDIO":
                cur_track = None   # データトラックは無視
                continue
            cur_file.tracks.append(cur_track)
        elif key == "TITLE" and len(tk) >= 2:
            if cur_track is not None:
                cur_track.title = tk[1]
            elif not sheet.files:
                sheet.album = tk[1]
        elif key == "PERFORMER" and len(tk) >= 2:
            if cur_track is not None:
                cur_track.performer = tk[1]
            elif not sheet.files:
                sheet.performer = tk[1]
        elif key == "ISRC" and cur_track is not None and len(tk) >= 2:
            cur_track.isrc = tk[1]
        elif key == "INDEX" and cur_track is not None and len(tk) >= 3:
            try:
                idx = int(tk[1])
            except ValueError:
                continue
            fr = _msf_to_frames(tk[2])
            if idx == 0:
                cur_track.index00 = fr
            elif idx == 1:
                cur_track.index01 = fr
    for f in sheet.files:
        f.tracks = [t for t in f.tracks]
    return sheet


def parse_cue_file(path: str | Path) -> CueSheet:
    return parse_cue_text(decode_text(Path(path).read_bytes()))


def resolve_audio_file(cue_dir: Path, name: str) -> Path | None:
    """CUE が指す音声ファイルを実在ファイルに解決する(拡張子違い・大小文字違いに対応)。"""
    name = name.replace("\\", "/")
    base = Path(name).name
    cand = cue_dir / base
    if cand.is_file():
        return cand
    try:
        entries = list(cue_dir.iterdir())
    except OSError:
        return None
    lower = base.lower()
    for e in entries:
        if e.name.lower() == lower and e.is_file():
            return e
    stem = Path(base).stem.lower()
    exts = sorted(AUDIO_EXTS, key=lambda x: (x != ".flac", x))
    for ext in exts:
        for e in entries:
            if e.is_file() and e.stem.lower() == stem and e.suffix.lower() == ext:
                return e
    return None


def read_embedded_cue(path: str | Path) -> CueSheet | None:
    """FLAC の埋め込み CUE を読む。無ければ None。"""
    p = Path(path)
    if p.suffix.lower() != ".flac":
        return None
    try:
        from mutagen.flac import FLAC

        fl = FLAC(str(p))
    except Exception:
        return None
    tags = fl.tags
    if tags:
        for key in ("CUESHEET", "cuesheet"):
            vals = tags.get(key)
            if vals:
                sheet = parse_cue_text("\n".join(vals))
                if sheet.files and sheet.files[0].tracks:
                    # 埋め込みでは FILE 行が無いか、元ファイル名のことがある。実体は自分自身。
                    for f in sheet.files:
                        f.name = p.name
                    return sheet
                # FILE 行が無い CUE テキスト
                sheet2 = parse_cue_text(f'FILE "{p.name}" WAVE\n' + "\n".join(vals))
                if sheet2.files and sheet2.files[0].tracks:
                    return sheet2
    cs = getattr(fl, "cuesheet", None)
    if cs is not None and getattr(cs, "tracks", None):
        rate = fl.info.sample_rate or 44100
        tracks: list[CueTrack] = []
        for t in cs.tracks:
            if t.track_number >= 170:  # lead-out
                continue
            idx01 = None
            for ix in getattr(t, "indexes", []):
                if ix.index_number == 1:
                    idx01 = ix.index_offset
            start = t.start_offset + (idx01 or 0)
            tracks.append(CueTrack(number=t.track_number, index01=int(start * CD_FRAMES_PER_SEC / rate)))
        if tracks:
            # FLAC の CUESHEET ブロックはサンプル精度。精度を保つため後段で frames->samples に変換される。
            sheet3 = CueSheet(files=[CueFile(name=p.name, tracks=tracks)])
            return sheet3
    return None


def frames_to_samples(cd_frames: int, rate: int) -> int:
    return int(round(cd_frames * rate / CD_FRAMES_PER_SEC))
