"""python -m miyamizu で GUI を起動する。"""
import sys

from PyQt6.QtGui import QColor, QPalette
from PyQt6.QtWidgets import QApplication

from .gui.main_window import MainWindow


def apply_dark(app: QApplication):
    app.setStyle("Fusion")
    p = QPalette()
    c = QColor
    p.setColor(QPalette.ColorRole.Window, c(36, 38, 43))
    p.setColor(QPalette.ColorRole.WindowText, c(225, 225, 225))
    p.setColor(QPalette.ColorRole.Base, c(28, 30, 34))
    p.setColor(QPalette.ColorRole.AlternateBase, c(36, 38, 43))
    p.setColor(QPalette.ColorRole.Text, c(225, 225, 225))
    p.setColor(QPalette.ColorRole.Button, c(48, 51, 57))
    p.setColor(QPalette.ColorRole.ButtonText, c(225, 225, 225))
    p.setColor(QPalette.ColorRole.ToolTipBase, c(48, 51, 57))
    p.setColor(QPalette.ColorRole.ToolTipText, c(225, 225, 225))
    p.setColor(QPalette.ColorRole.Highlight, c(60, 150, 125))
    p.setColor(QPalette.ColorRole.HighlightedText, c(255, 255, 255))
    p.setColor(QPalette.ColorRole.PlaceholderText, c(130, 134, 140))
    app.setPalette(p)


def _install_crash_log():
    """原因不明の落ち方を調べられるよう、致命的エラーと未処理の例外をデータフォルダの crash.log に残す。"""
    try:
        import faulthandler
        import threading
        import time
        import traceback

        from .common import data_dir
        d = data_dir()
        d.mkdir(parents=True, exist_ok=True)
        f = open(d / "crash.log", "a", buffering=1)
        f.write(f"\n--- {time.strftime('%Y-%m-%d %H:%M:%S')} 起動 ---\n")
        faulthandler.enable(file=f, all_threads=True)

        def hook(et, ev, tb):
            f.write("".join(traceback.format_exception(et, ev, tb)))
            sys.__excepthook__(et, ev, tb)

        def thook(a):
            f.write(f"スレッド {a.thread.name if a.thread else '?'}: " + "".join(traceback.format_exception(a.exc_type, a.exc_value, a.exc_traceback)))
        sys.excepthook = hook
        threading.excepthook = thook
        return f
    except Exception:  # noqa: BLE001
        return None


def main():
    _install_crash_log()
    app = QApplication(sys.argv)
    app.setApplicationName("Miyamizu")
    app.setApplicationDisplayName("Miyamizu")
    app.setDesktopFileName("miyamizu")
    from PyQt6.QtGui import QIcon
    from .common import app_icon
    app.setWindowIcon(app_icon())
    apply_dark(app)
    w = MainWindow()
    w.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
