"""Runs analyses in the background and asks the user when a check needs a decision."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

from PySide6.QtCore import QObject, Signal
from PySide6.QtWidgets import QMessageBox, QWidget

from ..core.analysis import AnalysisOptions, Cancelled, NeedsDecision, analyze
from ..core.result import CaptureResult
from ..core.session import Session
from .widgets import run_in_background


@dataclass
class Job:
    label: str
    session: Session
    produce: Callable[[AnalysisOptions], CaptureResult]  # runs in a worker thread
    options: AnalysisOptions = field(default_factory=AnalysisOptions)


def file_job(path, session: Session, how: str = "") -> Job:
    from ..core.audio_io import read_audio
    from ..core.checks import CheckResult, Severity

    def produce(opts: AnalysisOptions) -> CaptureResult:
        if how:
            sev = Severity.INFO if "most recent" in how or "chosen" in how else Severity.OK
            opts.extra_checks = [CheckResult("session", sev, how)]
        return analyze(session, read_audio(path), opts)

    return Job(str(path), session, produce)


def ask_decision(parent: QWidget, label: str, decision) -> str:
    box = QMessageBox(parent)
    box.setIcon(QMessageBox.Icon.Warning)
    box.setWindowTitle("Your choice is needed")
    box.setText(f"<b>{label}</b>")
    box.setInformativeText(decision.question)
    buttons = {}
    for value, text in decision.choices:
        b = box.addButton(text, QMessageBox.ButtonRole.AcceptRole)
        buttons[b] = value
        if value == decision.default:
            box.setDefaultButton(b)
    cancel = box.addButton("Cancel", QMessageBox.ButtonRole.RejectRole)
    buttons[cancel] = "cancel"
    box.exec()
    return buttons.get(box.clickedButton(), "cancel")


class AnalysisRunner(QObject):
    progress = Signal(str)
    finished = Signal(list)  # list[CaptureResult]
    failed = Signal(str, str)  # label, message

    def __init__(self, parent: QWidget):
        super().__init__(parent)
        self.parent_widget = parent
        self.queue: list[Job] = []
        self.results: list[CaptureResult] = []
        self.busy = False

    def run(self, jobs: list[Job]) -> None:
        self.queue = list(jobs)
        self.results = []
        self.busy = True
        self._next()

    def _next(self) -> None:
        if not self.queue:
            self.busy = False
            self.finished.emit(self.results)
            return
        job = self.queue[0]
        self.progress.emit(f"Analyzing {job.label}…")
        run_in_background(job.produce, job.options, on_done=self._done, on_error=self._error)

    def _done(self, result: CaptureResult) -> None:
        self.queue.pop(0)
        self.results.append(result)
        self._next()

    def _error(self, exc: Exception, tb: str) -> None:
        job = self.queue[0]
        if isinstance(exc, NeedsDecision):
            choice = ask_decision(self.parent_widget, job.label, exc.decision)
            if choice != "cancel":
                job.options.decisions[exc.decision.key] = choice
                self._next()
                return
            exc = Cancelled()
        self.queue.pop(0)
        if not isinstance(exc, Cancelled):
            self.failed.emit(job.label, str(exc) or type(exc).__name__)
        self._next()
