"""Plots (pyqtgraph): IR waveform, magnitude response, energy decay, null-test error spectrum."""

from __future__ import annotations

import numpy as np
import pyqtgraph as pg
from PySide6.QtGui import QPalette
from PySide6.QtWidgets import QApplication, QComboBox, QHBoxLayout, QLabel, QVBoxLayout, QWidget

from ..core.metrics import energy_decay_curve, magnitude_response
from ..core.result import CaptureResult

CHANNEL_COLORS = ["#3478c6", "#d0342c", "#2e9d4f", "#d48a00"]
pg.setConfigOptions(antialias=True)


def apply_theme(plot: pg.PlotWidget) -> None:
    pal = QApplication.palette()
    plot.setBackground(pal.color(QPalette.ColorRole.Base))
    fg = pal.color(QPalette.ColorRole.Text)
    for ax in ("left", "bottom"):
        a = plot.getAxis(ax)
        a.setPen(fg)
        a.setTextPen(fg)


def _plot(title: str, xlabel: str, ylabel: str) -> pg.PlotWidget:
    w = pg.PlotWidget()
    w.setTitle(title)
    w.setLabel("bottom", xlabel)
    w.setLabel("left", ylabel)
    w.showGrid(x=True, y=True, alpha=0.25)
    w.addLegend(offset=(-10, 10))
    apply_theme(w)
    return w


class IRPlot(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.plot = _plot("Impulse response", "time (ms)", "amplitude")
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.addWidget(self.plot)

    def show_result(self, r: CaptureResult) -> None:
        self.plot.clear()
        if r.ir is None:
            return
        apply_theme(self.plot)
        ch = r.ir_channels()
        t = (np.arange(ch.shape[1]) + r.ir_start) / r.sample_rate * 1000
        step = max(1, ch.shape[1] // 20000)
        for i, name in enumerate(r.channel_names):
            self.plot.plot(t[::step], ch[i, ::step], pen=pg.mkPen(CHANNEL_COLORS[i % 4], width=1), name=name)


class MagnitudePlot(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.plot = _plot("Magnitude response", "frequency (Hz)", "dB")
        self.plot.setLogMode(x=True, y=False)
        self.smoothing = QComboBox()
        self.smoothing.addItems(["no smoothing", "1/12 octave", "1/6 octave", "1/3 octave"])
        self.smoothing.setCurrentIndex(2)
        self.smoothing.currentIndexChanged.connect(lambda _: self._draw())
        top = QHBoxLayout()
        top.addStretch()
        top.addWidget(QLabel("Smoothing:"))
        top.addWidget(self.smoothing)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.addLayout(top)
        lay.addWidget(self.plot)
        self._result: CaptureResult | None = None

    def show_result(self, r: CaptureResult) -> None:
        self._result = r
        self._draw()

    def _draw(self) -> None:
        self.plot.clear()
        r = self._result
        if r is None or r.ir is None:
            return
        apply_theme(self.plot)
        oct_ = [None, 1 / 12, 1 / 6, 1 / 3][self.smoothing.currentIndex()]
        gain = 10 ** (-r.normalization_gain_db / 20) if r.normalization.value != "off" else 1.0
        f_lo = max(10.0, r.session.settings.f_start_hz / 2)
        for i, name in enumerate(r.channel_names):
            f, m = magnitude_response(r.ir_channels()[i] * gain, r.sample_rate, oct_, f_min=f_lo)
            self.plot.plot(f, m, pen=pg.mkPen(CHANNEL_COLORS[i % 4], width=1.5), name=name)
        self.plot.setTitle("Magnitude response" + (" (before normalization)" if gain != 1.0 else ""))


class DecayPlot(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.plot = _plot("Energy decay curve (Schroeder)", "time (s)", "dB")
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.addWidget(self.plot)

    def show_result(self, r: CaptureResult) -> None:
        self.plot.clear()
        if r.ir is None:
            return
        apply_theme(self.plot)
        ch = r.ir_channels()
        step = max(1, ch.shape[1] // 20000)
        t = np.arange(ch.shape[1]) / r.sample_rate
        for i, name in enumerate(r.channel_names):
            edc = energy_decay_curve(ch[i])
            self.plot.plot(t[::step], edc[::step], pen=pg.mkPen(CHANNEL_COLORS[i % 4], width=1.5), name=name)
        self.plot.setYRange(-100, 0)
        title = "Energy decay curve (Schroeder)"
        if r.rt60_s:
            title += f" — RT60 ≈ {r.rt60_s:.2f} s"
        self.plot.setTitle(title)


class ErrorSpectrumPlot(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.plot = _plot("Null test: residual re rendered level", "frequency (Hz)", "dB")
        self.plot.setLogMode(x=True, y=False)
        self.bands = QLabel()
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.addWidget(self.plot)
        lay.addWidget(self.bands)

    def show_result(self, r: CaptureResult) -> None:
        self.plot.clear()
        nt = r.null_test
        if nt is None or nt.spectrum_freqs is None:
            self.bands.setText("No null test (verification part missing).")
            return
        apply_theme(self.plot)
        sel = (nt.spectrum_freqs >= r.session.settings.f_start_hz) & (nt.spectrum_freqs > 0)
        self.plot.plot(nt.spectrum_freqs[sel], np.maximum(nt.spectrum_error_db[sel], -160),
                       pen=pg.mkPen(CHANNEL_COLORS[0], width=1.5), name="error")
        for y, c in ((-60, "#2e9d4f"), (-40, "#d48a00"), (-25, "#d0342c")):
            self.plot.addLine(y=y, pen=pg.mkPen(c, width=1, style=pg.QtCore.Qt.PenStyle.DashLine))
        self.bands.setText("Per band: " + ", ".join(f"{k}: {v:.1f} dB" for k, v in nt.bands_db.items()))
