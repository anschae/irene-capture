"""Step 3: Results — batch table, checks and conversions, plots, null test, file creation."""

from __future__ import annotations

import html
from pathlib import Path

from PySide6.QtCore import Qt, QUrl, Signal
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QAbstractItemView,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMessageBox,
    QPushButton,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QTextBrowser,
    QVBoxLayout,
    QWidget,
)

from ..core.checks import Severity
from ..core.report import write_outputs
from ..core.result import CaptureResult
from .plots import DecayPlot, ErrorSpectrumPlot, IRPlot, MagnitudePlot
from .widgets import SEVERITY_COLORS, RatingBadge, severity_html, severity_icon


def checks_html(r: CaptureResult) -> str:
    def row(c):
        detail = f'<br><span style="color:gray">{html.escape(c.detail)}</span>' if c.detail else ""
        return (f'<tr><td valign="top" style="padding-right:6px">{severity_html(c.severity)}</td>'
                f"<td>{html.escape(c.message)}{detail}</td></tr>")

    parts = ["<h3>Checks</h3><table>"]
    parts += [row(c) for c in r.checks if not c.conversion]
    parts.append("</table>")
    conv = r.conversions
    parts.append("<h3>Conversions</h3>")
    if conv:
        parts.append("<table>" + "".join(row(c) for c in conv) + "</table>")
    else:
        parts.append("<p>None — the rendered audio was used as it is.</p>")
    if r.hints:
        parts.append("<h3>What this means</h3><ul>" + "".join(f"<li>{html.escape(h)}</li>" for h in r.hints)
                     + "</ul>")
    if r.written:
        parts.append("<h3>Files written</h3><ul>" + "".join(f"<li>{html.escape(str(p))}</li>" for p in r.written)
                     + "</ul>")
    return "".join(parts)


class ResultsPage(QWidget):
    capture_again = Signal()
    status = Signal(str)

    def __init__(self, setup, parent=None):
        super().__init__(parent)
        self.setup = setup
        self.results: list[CaptureResult] = []

        self.table = QTableWidget(0, 5)
        self.table.setHorizontalHeaderLabels(["File / capture", "Rating", "Null test", "Status", "IR file"])
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.itemSelectionChanged.connect(self._selected)
        self.table.setMaximumHeight(160)

        self.title = QLabel()
        self.title.setTextFormat(Qt.TextFormat.RichText)
        self.badge = RatingBadge()
        self.checks = QTextBrowser()
        self.plots = QTabWidget()
        self.ir_plot = IRPlot()
        self.mag_plot = MagnitudePlot()
        self.decay_plot = DecayPlot()
        self.err_plot = ErrorSpectrumPlot()
        self.plots.addTab(self.ir_plot, "IR")
        self.plots.addTab(self.mag_plot, "Frequency response")
        self.plots.addTab(self.decay_plot, "Decay")
        self.plots.addTab(self.err_plot, "Null test")

        self.create_btn = QPushButton("Create IR files")
        self.create_btn.setDefault(True)
        self.create_btn.clicked.connect(self.create_files)
        self.create_all_btn = QPushButton("Create all")
        self.create_all_btn.clicked.connect(self.create_all)
        self.open_btn = QPushButton("Open output folder")
        self.open_btn.clicked.connect(self.open_folder)
        again = QPushButton("Capture again")
        again.clicked.connect(self.capture_again)

        head = QHBoxLayout()
        head.addWidget(self.title, 1)
        head.addWidget(self.badge)
        split = QSplitter(Qt.Orientation.Horizontal)
        split.addWidget(self.checks)
        split.addWidget(self.plots)
        split.setSizes([380, 620])
        buttons = QHBoxLayout()
        buttons.addWidget(again)
        buttons.addStretch()
        buttons.addWidget(self.open_btn)
        buttons.addWidget(self.create_all_btn)
        buttons.addWidget(self.create_btn)
        lay = QVBoxLayout(self)
        lay.addWidget(self.table)
        lay.addLayout(head)
        lay.addWidget(split, 1)
        lay.addLayout(buttons)
        self.empty = QLabel("No results yet. Capture something first.")
        self._update_buttons()

    # --- data -------------------------------------------------------------------------------
    def add_results(self, results: list[CaptureResult]) -> None:
        self.results.extend(results)
        for r in results:
            row = self.table.rowCount()
            self.table.insertRow(row)
            sev = r.severity
            name = QTableWidgetItem(r.source_name if not r.plugin_info else
                                    f"{r.plugin_info.get('name')} — {r.session.settings.capture_name or 'capture'}")
            name.setIcon(severity_icon(sev))
            self.table.setItem(row, 0, name)
            self.table.setItem(row, 1, QTableWidgetItem(r.rating))
            self.table.setItem(row, 2, QTableWidgetItem(f"{r.null_test.residual_db:.1f} dB" if r.null_test else "—"))
            counts = {s: sum(c.severity == s for c in r.checks) for s in (Severity.ERROR, Severity.WARNING)}
            status = ("errors: " + str(counts[Severity.ERROR])) if counts[Severity.ERROR] else \
                ("warnings: " + str(counts[Severity.WARNING])) if counts[Severity.WARNING] else "OK"
            self.table.setItem(row, 3, QTableWidgetItem(status))
            self.table.setItem(row, 4, QTableWidgetItem("not yet" if r.ok else "—"))
        if results:
            self.table.selectRow(self.table.rowCount() - len(results))
        self._update_buttons()

    def current(self) -> CaptureResult | None:
        rows = self.table.selectionModel().selectedRows() if self.table.selectionModel() else []
        if not rows:
            return None
        return self.results[rows[0].row()]

    def _selected(self) -> None:
        r = self.current()
        self._update_buttons()
        if r is None:
            return
        s = r.session.settings
        extra = []
        if r.ir is not None:
            extra.append(f"{r.length_samples / r.sample_rate:.3f} s, {', '.join(r.channel_names)}")
        if r.distortion:
            extra.append(f"THD ≈ {r.distortion.thd_percent:.3f} %")
        if r.rt60_s:
            extra.append(f"RT60 ≈ {r.rt60_s:.2f} s")
        color = SEVERITY_COLORS[r.severity]
        self.title.setText(f'<b style="font-size:14pt">{html.escape(r.source_name)}</b><br>'
                           f"{s.capture_type.value}, {s.method.value}, {s.sample_rate / 1000:g} kHz — "
                           + " · ".join(extra)
                           + (f' <span style="color:{color}">(not usable, see errors)</span>' if not r.ok else ""))
        self.badge.set_rating(r.null_test.rating if r.null_test else None,
                              r.null_test.residual_db if r.null_test else None)
        self.checks.setHtml(checks_html(r))
        for p in (self.ir_plot, self.mag_plot, self.decay_plot, self.err_plot):
            p.show_result(r)
        self.plots.setTabVisible(2, r.ir is not None and (s.capture_type.value == "reverb" or
                                                          r.length_samples > 0.3 * r.sample_rate))

    def _update_buttons(self) -> None:
        r = self.current()
        self.create_btn.setEnabled(bool(r and r.ok and not r.written))
        self.create_all_btn.setVisible(len(self.results) > 1)
        self.create_all_btn.setEnabled(any(x.ok and not x.written for x in self.results))
        self.open_btn.setEnabled(bool(r and (r.written or r.source_path)))

    def _out_dir(self, r: CaptureResult) -> Path:
        if r.source_path is not None:
            return r.source_path.parent
        return self.setup.output_folder()

    def _write(self, r: CaptureResult) -> bool:
        try:
            files = write_outputs(r, self._out_dir(r))
        except (OSError, ValueError) as e:
            QMessageBox.critical(self, "Can't write the IR", str(e))
            return False
        row = self.results.index(r)
        self.table.setItem(row, 4, QTableWidgetItem(", ".join(p.name for p in files.ir_files)))
        self.status.emit("Wrote " + ", ".join(str(p) for p in files.ir_files))
        return True

    def create_files(self) -> None:
        r = self.current()
        if r and r.ok and not r.written and self._write(r):
            self._selected()

    def create_all(self) -> None:
        for r in self.results:
            if r.ok and not r.written and not self._write(r):
                break
        self._selected()

    def open_folder(self) -> None:
        r = self.current()
        if r is None:
            return
        folder = r.written[0].parent if r.written else self._out_dir(r)
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(folder)))
