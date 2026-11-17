"""Small reusable widgets and helpers."""

from __future__ import annotations

import html
import traceback
from collections.abc import Callable

from PySide6.QtCore import QObject, QRunnable, Qt, QThreadPool, Signal
from PySide6.QtGui import QColor, QIcon, QPainter, QPixmap
from PySide6.QtWidgets import QFrame, QLabel, QSizePolicy, QToolButton, QVBoxLayout, QWidget

from ..core.checks import Severity

SEVERITY_COLORS = {
    Severity.OK: "#2e9d4f",
    Severity.INFO: "#3478c6",
    Severity.WARNING: "#d48a00",
    Severity.ERROR: "#d0342c",
}
SEVERITY_SYMBOL = {Severity.OK: "✓", Severity.INFO: "i", Severity.WARNING: "!", Severity.ERROR: "✕"}
RATING_COLORS = {
    "excellent": "#2e9d4f",
    "very good": "#6aa84f",
    "audible differences possible": "#d48a00",
    "unusable": "#d0342c",
}


def severity_icon(sev: Severity, size: int = 16) -> QIcon:
    pm = QPixmap(size, size)
    pm.fill(Qt.GlobalColor.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    p.setBrush(QColor(SEVERITY_COLORS[sev]))
    p.setPen(Qt.PenStyle.NoPen)
    p.drawEllipse(1, 1, size - 2, size - 2)
    p.setPen(QColor("white"))
    f = p.font()
    f.setBold(True)
    f.setPixelSize(int(size * 0.7))
    p.setFont(f)
    p.drawText(pm.rect(), Qt.AlignmentFlag.AlignCenter, SEVERITY_SYMBOL[sev])
    p.end()
    return QIcon(pm)


def severity_html(sev: Severity) -> str:
    return (f'<span style="color:{SEVERITY_COLORS[sev]}; font-weight:bold">'
            f'{html.escape(SEVERITY_SYMBOL[sev])}</span>')


class RatingBadge(QLabel):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.setMinimumWidth(160)
        self.set_rating(None)

    def set_rating(self, rating: str | None, residual_db: float | None = None) -> None:
        if not rating:
            self.setText("no null test")
            self.setStyleSheet("QLabel { border: 1px solid palette(mid); border-radius: 6px; padding: 6px 10px; }")
            return
        color = RATING_COLORS.get(rating, "#777")
        text = rating if residual_db is None else f"{rating}  ({residual_db:.1f} dB)"
        self.setText(text)
        self.setStyleSheet(f"QLabel {{ background: {color}; color: white; font-weight: bold; border-radius: 6px;"
                           " padding: 6px 10px; }")


class CollapsibleSection(QWidget):
    """A header button that shows/hides its content."""

    def __init__(self, title: str, content: QWidget, expanded: bool = False, parent=None):
        super().__init__(parent)
        self.button = QToolButton(text=title, checkable=True, checked=expanded)
        self.button.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self.button.setStyleSheet("QToolButton { border: none; font-weight: bold; }")
        self.button.toggled.connect(self._toggle)
        self.content = content
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.addWidget(self.button)
        line = QFrame()
        line.setFrameShape(QFrame.Shape.HLine)
        line.setFrameShadow(QFrame.Shadow.Sunken)
        lay.addWidget(line)
        lay.addWidget(content)
        self._toggle(expanded)

    def _toggle(self, on: bool) -> None:
        self.button.setArrowType(Qt.ArrowType.DownArrow if on else Qt.ArrowType.RightArrow)
        self.content.setVisible(on)


class MessageLabel(QLabel):
    """Inline validation message (word-wrapped, colored by severity)."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWordWrap(True)
        self.setTextFormat(Qt.TextFormat.RichText)
        self.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Minimum)
        self.hide()

    def set_messages(self, messages) -> None:
        if not messages:
            self.clear()
            self.hide()
            return
        rows = [f'{severity_html(m.severity)} <span style="color:{SEVERITY_COLORS[m.severity]}">'
                f"{html.escape(m.text)}</span>" for m in messages]
        self.setText("<br>".join(rows))
        self.show()


# --- background work ---------------------------------------------------------------------


class _Signals(QObject):
    done = Signal(object)
    failed = Signal(object, str)


class Task(QRunnable):
    def __init__(self, fn: Callable, *args, **kwargs):
        super().__init__()
        self.fn, self.args, self.kwargs = fn, args, kwargs
        self.signals = _Signals()

    def run(self) -> None:
        try:
            r = self.fn(*self.args, **self.kwargs)
        except Exception as e:  # delivered to the GUI thread
            self.signals.failed.emit(e, traceback.format_exc())
            return
        self.signals.done.emit(r)


_running: set[Task] = set()


def run_in_background(fn: Callable, *args, on_done: Callable | None = None,
                      on_error: Callable | None = None, **kwargs) -> Task:
    """Run ``fn`` on the thread pool; callbacks are called in the GUI thread."""
    task = Task(fn, *args, **kwargs)
    _running.add(task)
    task.setAutoDelete(False)

    def finish(*_):
        _running.discard(task)

    if on_done:
        task.signals.done.connect(on_done)
    if on_error:
        task.signals.failed.connect(on_error)
    task.signals.done.connect(finish)
    task.signals.failed.connect(finish)
    QThreadPool.globalInstance().start(task)
    return task
