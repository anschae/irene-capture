"""QApplication setup."""

from __future__ import annotations

import sys


def run_gui() -> int:
    from PySide6.QtGui import QIcon
    from PySide6.QtWidgets import QApplication

    from ..paths import resource_path
    from .main_window import MainWindow

    QApplication.setOrganizationName("anschae")
    QApplication.setApplicationName("irene-capture")
    app = QApplication.instance() or QApplication(sys.argv)
    # Follow the system light/dark theme: Qt 6.5+ does this with the native styles on macOS and
    # Windows 11; Fusion follows the color scheme on Windows 10 and Linux.
    if sys.platform == "win32" and app.style().name().lower() not in ("windows11",):
        app.setStyle("Fusion")
    icon = resource_path("icon.png")
    if icon.exists():
        app.setWindowIcon(QIcon(str(icon)))
    w = MainWindow()
    w.show()
    return app.exec()
