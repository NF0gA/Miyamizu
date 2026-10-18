"""Miyamizu の起動口(PyInstaller のエントリ)。--engine なら再生エンジン、それ以外は GUI。"""
import multiprocessing
import sys


def main():
    multiprocessing.freeze_support()          # 準備プロセス(spawn)を凍結版で動かすために必須
    if "--engine" in sys.argv:
        sys.argv.remove("--engine")
        from miyamizu.engine import main as engine_main
        engine_main(sys.argv[1:])
    else:
        from miyamizu.__main__ import main as gui_main
        gui_main()


if __name__ == "__main__":
    main()
