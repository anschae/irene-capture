"""Main window: three steps (Setup, Capture, Results) in a non-modal window."""

from __future__ import annotations

from PySide6.QtCore import QSettings, QUrl
from PySide6.QtGui import QAction, QDesktopServices
from PySide6.QtWidgets import (
    QHBoxLayout,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMessageBox,
    QStackedWidget,
    QWidget,
)

from .. import build_info
from .capture_page import CapturePage
from .results_page import ResultsPage
from .runner import AnalysisRunner
from .setup_page import SetupPage

REPO = "https://github.com/anschae/irene-capture"
STEPS = ("1  Setup", "2  Capture", "3  Results")


class MainWindow(QMainWindow):
    def __init__(self, settings: QSettings | None = None):
        super().__init__()
        self.qs = settings or QSettings("anschae", "irene-capture")
        self.setWindowTitle("irene-capture")
        self.resize(1100, 760)

        self.steps = QListWidget()
        for s in STEPS:
            self.steps.addItem(QListWidgetItem(s))
        self.steps.setFixedWidth(140)
        self.steps.setStyleSheet("QListWidget { font-size: 12pt; } QListWidget::item { padding: 10px 6px; }")
        self.stack = QStackedWidget()
        self.setup = SetupPage(self.qs)
        self.capture = CapturePage(self.setup, self.qs)
        self.results = ResultsPage(self.setup)
        for w in (self.setup, self.capture, self.results):
            self.stack.addWidget(w)
        self.steps.currentRowChanged.connect(self.stack.setCurrentIndex)
        self.steps.setCurrentRow(0)

        self.setup.next_requested.connect(lambda: self.go(1))
        self.results.capture_again.connect(lambda: self.go(1))
        self.runner = AnalysisRunner(self)
        self.capture.analyze_requested.connect(self.runner.run)
        self.runner.progress.connect(self.show_status)
        self.runner.finished.connect(self._finished)
        self.runner.failed.connect(lambda label, msg: QMessageBox.critical(self, "Analysis failed",
                                                                          f"{label}\n\n{msg}"))
        for w in (self.capture, self.results):
            w.status.connect(self.show_status)

        central = QWidget()
        lay = QHBoxLayout(central)
        lay.addWidget(self.steps)
        lay.addWidget(self.stack, 1)
        self.setCentralWidget(central)
        self._menus()
        self.statusBar().showMessage(f"irene-capture {build_info()}")
        geo = self.qs.value("window/geometry")
        if geo is not None:
            self.restoreGeometry(geo)

    def _menus(self) -> None:
        m = self.menuBar().addMenu("&File")
        imp = QAction("Import rendered file(s)…", self)
        imp.setShortcut("Ctrl+O")
        imp.triggered.connect(lambda: (self.go(1), self.capture.tabs.setCurrentIndex(0),
                                       self.capture.daw.import_dialog()))
        m.addAction(imp)
        new = QAction("Create test file…", self)
        new.setShortcut("Ctrl+N")
        new.triggered.connect(lambda: (self.go(1), self.capture.tabs.setCurrentIndex(0),
                                       self.capture.daw.create_test_file()))
        m.addAction(new)
        m.addSeparator()
        quit_ = QAction("Quit", self)
        quit_.setShortcut("Ctrl+Q")
        quit_.setMenuRole(QAction.MenuRole.QuitRole)
        quit_.triggered.connect(self.close)
        m.addAction(quit_)
        h = self.menuBar().addMenu("&Help")
        guide = QAction("User guide (online)", self)
        guide.triggered.connect(lambda: QDesktopServices.openUrl(QUrl(REPO + "#readme")))
        h.addAction(guide)
        about = QAction("About irene-capture", self)
        about.setMenuRole(QAction.MenuRole.AboutRole)
        about.triggered.connect(self.about)
        h.addAction(about)

    def go(self, step: int) -> None:
        self.steps.setCurrentRow(step)

    def show_status(self, text: str) -> None:
        self.statusBar().showMessage(text, 15000)

    def _finished(self, results: list) -> None:
        if not results:
            self.show_status("Nothing analyzed.")
            return
        self.results.add_results(results)
        self.go(2)
        self.show_status(f"Analyzed {len(results)} file(s). Check the summary, then press Create IR files.")

    def about(self) -> None:
        QMessageBox.about(
            self, "About irene-capture",
            f"<h3>irene-capture {build_info()}</h3>"
            "<p>Captures impulse responses from audio plugins (cabinet sims, reverbs, EQs/filters) — the "
            "companion tool to the <a href='https://github.com/anschae/irene'>IRene</a> IR loader.</p>"
            "<p>Only linear, time-invariant processing can be captured as an IR.</p>"
            f"<p><a href='{REPO}'>{REPO}</a></p>"
            "<p>License: GNU GPL v3. Plugin hosting by Spotify's pedalboard (GPLv3), GUI by Qt for Python.</p>")

    def closeEvent(self, e) -> None:
        self.qs.setValue("window/geometry", self.saveGeometry())
        self.capture.shutdown()
        super().closeEvent(e)
