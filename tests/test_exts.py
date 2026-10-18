"""拡張子の許可/無視設定。"""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from miyamizu.common import active_exts, find_icon
from miyamizu.settings import Settings
f = 0
def check(n, ok):
    global f; print(("PASS " if ok else "FAIL ") + n); f += (not ok)
check("既定は全対応形式", ".flac" in active_exts(Settings()) and ".m4a" in active_exts(Settings()))
check("許可リストで絞れる", active_exts(Settings(enabled_exts=["flac", ".wav"])) == {".flac", ".wav"})
check("無視が許可より優先", active_exts(Settings(enabled_exts=[".flac", ".wav"], ignore_exts=["WAV"])) == {".flac"})
check("対応外の拡張子は増えない", ".jpg" not in active_exts(Settings(enabled_exts=[".jpg", ".flac"])))
check("icon.png が見つかる", find_icon() is not None and find_icon().name == "icon.png")
print("ALL PASS" if not f else f"{f} FAIL")
