# -*- mode: python ; coding: utf-8 -*-
# PyInstaller spec: pyinstaller miyamizu.spec
from PyInstaller.utils.hooks import collect_all

datas, binaries, hiddenimports = [], [], []
import sys as _sys
_pkgs = ["soundfile", "soxr", "av", "mutagen", "charset_normalizer"]
if _sys.platform == "win32":
    _pkgs += ["sounddevice", "_sounddevice_data"]       # PortAudio(WASAPI)
for pkg in _pkgs:
    d, b, h = collect_all(pkg)
    datas += d; binaries += b; hiddenimports += h
datas += [("icon.png", "."), ("miyamizu/assets", "miyamizu/assets"), ("miyamizu/nshape.c", "miyamizu"),
          ("miyamizu/native", "miyamizu/native"), ("miyamizu/native/R8BRAIN_LICENSE.txt", "."),
          ("LICENSE", "."), ("THIRD_PARTY_NOTICES.md", ".")]
import os as _os
_ext = ".dll" if _sys.platform == "win32" else ".so"
if _os.path.isfile("miyamizu/libmiyamizu_nshape" + _ext):
    binaries += [("miyamizu/libmiyamizu_nshape" + _ext, ".")]
hiddenimports += ["miyamizu.engine", "miyamizu.producer", "miyamizu.gui.main_window"]

if _os.path.isfile("miyamizu/libr8bshim" + _ext):      # build_r8brain.sh / build.bat で作る(無ければ実行時にビルドを試す)
    binaries += [("miyamizu/libr8bshim" + _ext, ".")]
a = Analysis(["miyamizu.py"], pathex=["."], binaries=binaries, datas=datas, hiddenimports=hiddenimports,
             excludes=["tkinter", "matplotlib", "scipy", "PyQt6.QtWebEngineCore", "PyQt6.QtWebEngineWidgets", "PyQt6.QtQml", "PyQt6.QtQuick"])
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name="Miyamizu", console=False, icon="miyamizu.ico")
coll = COLLECT(exe, a.binaries, a.datas, strip=False, upx=False, name="Miyamizu")
