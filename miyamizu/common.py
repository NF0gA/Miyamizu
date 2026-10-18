"""共通の定数・パス・小さなユーティリティ。"""
from __future__ import annotations

import os
import sys
from pathlib import Path

APP_NAME = "miyamizu"
IS_WIN = sys.platform == "win32"

# 取り扱う拡張子
AUDIO_EXTS = {".flac", ".wav", ".mp3", ".m4a", ".aac", ".ogg", ".opus", ".wv", ".ape", ".alac", ".aiff", ".aif"}
# soundfile(libsndfile) で開く拡張子。それ以外は PyAV(ffmpeg) を使う。
SF_EXTS = {".flac", ".wav", ".mp3", ".ogg", ".aiff", ".aif", ".opus"}
AV_EXTS = {".m4a", ".aac", ".alac", ".wv", ".ape"}

def active_exts(settings) -> set[str]:
    """設定(取り込む拡張子/無視する拡張子)から実際にスキャンする拡張子の集合を返す。"""
    en = {e.lower() if e.startswith(".") else "." + e.lower() for e in (settings.enabled_exts or [])} or set(AUDIO_EXTS)
    ig = {e.lower() if e.startswith(".") else "." + e.lower() for e in (settings.ignore_exts or [])}
    return (en & AUDIO_EXTS) - ig


STD_RATES = [44100, 48000, 88200, 96000, 176400, 192000, 352800, 384000]
RATES_44K = {44100, 88200, 176400, 352800}
RATES_48K = {48000, 96000, 192000, 384000}


def rate_family(rate: int) -> int:
    """44.1kHz 系なら 44100、48kHz 系なら 48000、それ以外は 0。"""
    if rate in RATES_44K or rate % 44100 == 0:
        return 44100
    if rate in RATES_48K or rate % 48000 == 0:
        return 48000
    return 0


OLD_APP_NAME = "hifiplayer"          # 旧名(Hifi Player)。初回だけ設定/ライブラリを引き継ぐ


def _migrated(base: Path) -> Path:
    d = base / APP_NAME
    old = base / OLD_APP_NAME
    if not d.exists() and old.is_dir():
        try:
            old.rename(d)               # 設定・ライブラリDB・復元用ファイルをそのまま引き継ぐ
        except OSError:
            pass
    d.mkdir(parents=True, exist_ok=True)
    return d


def use_tcp() -> bool:
    """エンジンとの接続に TCP(127.0.0.1)を使う。Windows は常に TCP。Linux でも MIYAMIZU_TCP=1 で試せる(テスト用)。"""
    return sys.platform == "win32" or bool(os.environ.get("MIYAMIZU_TCP"))


def config_dir() -> Path:
    if sys.platform == "win32":
        return _migrated(Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming")))
    return _migrated(Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")))


def cache_dir() -> Path:
    if sys.platform == "win32":
        d = data_dir() / "cache"
        d.mkdir(parents=True, exist_ok=True)
        return d
    return _migrated(Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")))


def data_dir() -> Path:
    if sys.platform == "win32":
        return _migrated(Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local")))
    return _migrated(Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share")))


def runtime_dir() -> Path:
    if sys.platform == "win32":
        import tempfile
        d = Path(tempfile.gettempdir()) / f"{APP_NAME}-{os.environ.get('USERNAME', 'user')}"
        d.mkdir(parents=True, exist_ok=True)
        return d
    base = os.environ.get("XDG_RUNTIME_DIR")
    if base and os.access(base, os.W_OK):
        d = Path(base)
    else:
        d = Path("/tmp") / f"{APP_NAME}-{os.getuid()}"
        d.mkdir(parents=True, exist_ok=True)
        os.chmod(d, 0o700)
    return d


def default_socket_path() -> str:
    """UNIX ソケットのパス。TCP 接続の時は、ポートと認証トークンを書いたファイルのパス。"""
    return str(runtime_dir() / (f"{APP_NAME}-engine.port" if use_tcp() else f"{APP_NAME}-engine.sock"))


def fmt_time(sec: float) -> str:
    if sec is None or sec < 0:
        return "--:--"
    sec = int(sec + 0.5)
    h, rem = divmod(sec, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def rate_label(rate: int) -> str:
    return f"{rate / 1000:g}kHz"


def app_dirs() -> list[Path]:
    """icon.png などを探すフォルダ(優先順): 実行ファイルの隣 → カレント → ソース直下 → PyInstaller 展開先。"""
    import sys

    out: list[Path] = []
    if getattr(sys, "frozen", False):
        out.append(Path(sys.executable).resolve().parent)
    out.append(Path.cwd())
    out.append(Path(__file__).resolve().parent.parent)
    mp = getattr(sys, "_MEIPASS", None)
    if mp:
        out.append(Path(mp))
    seen, res = set(), []
    for d in out:
        if d not in seen:
            seen.add(d)
            res.append(d)
    return res


def find_icon() -> Path | None:
    """icon.png(実行ファイルの隣 → カレント → ソース直下 → 展開先の順)。無ければ同梱の既定アイコン。"""
    for d in app_dirs():
        p = d / "icon.png"
        if p.is_file():
            return p
    return default_icon()


def assets_dir() -> Path:
    import sys

    mp = getattr(sys, "_MEIPASS", None)
    cand = [Path(mp) / "miyamizu" / "assets"] if mp else []
    cand.append(Path(__file__).resolve().parent / "assets")
    for c in cand:
        if c.is_dir():
            return c
    return cand[-1]


def default_icon() -> Path | None:
    p = assets_dir() / "icon-512.png"
    return p if p.is_file() else None


def app_icon():
    """QIcon を返す。ユーザーの icon.png があればそれ(1枚)、無ければ同梱の既定(各サイズ)。"""
    from PyQt6.QtGui import QIcon

    ic = find_icon()
    dflt = default_icon()
    if ic is not None and dflt is not None and ic.resolve() != dflt.resolve():
        user = ic
        # ソース直下/展開先に置いた既定と同一内容なら、サイズ別の既定を使う(小さい表示を綺麗に)
        try:
            if user.read_bytes() != dflt.read_bytes():
                return QIcon(str(user))
        except OSError:
            return QIcon(str(user))
    q = QIcon()
    for f in sorted(assets_dir().glob("icon-*.png")):
        q.addFile(str(f))
    return q


def purge_old_downloads() -> None:
    """旧版が保存していたダウンロード済みファイル(cache/streams)を削除する。現在のMiyamizuはネットから何も保存しない。"""
    import shutil

    try:
        shutil.rmtree(cache_dir() / "streams", ignore_errors=True)
    except OSError:
        pass
