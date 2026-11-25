"""Step 1: Setup — capture type and all settings, with inline validation."""

from __future__ import annotations

import json
from pathlib import Path

from PySide6.QtCore import QSettings, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from ..core.presets import (
    PRESETS,
    SAMPLE_RATES,
    BitDepth,
    CaptureSettings,
    CaptureType,
    Method,
    Normalization,
    StereoMode,
    TrueStereoOutput,
    validate,
)
from .widgets import CollapsibleSection, MessageLabel

SETTINGS_VERSION = 2  # 2: cabinet preset without tail trim
STEREO_LABELS = {StereoMode.MONO: "Mono", StereoMode.STEREO: "Stereo (mono in → stereo out)",
                 StereoMode.TRUE_STEREO: "True stereo (L, then R: 4 IRs)"}


def _spin(lo, hi, decimals, step, suffix) -> QDoubleSpinBox:
    s = QDoubleSpinBox()
    s.setRange(lo, hi)
    s.setDecimals(decimals)
    s.setSingleStep(step)
    s.setSuffix(suffix)
    s.setKeyboardTracking(False)
    return s


class SetupPage(QWidget):
    changed = Signal()
    next_requested = Signal()

    def __init__(self, settings: QSettings, parent=None):
        super().__init__(parent)
        self.qs = settings
        self._loading = False

        self.type = QComboBox()
        for t, p in PRESETS.items():
            self.type.addItem(p.label, t.value)
        self.method = QComboBox()
        self.method.addItem("Exponential sine sweep (recommended)", Method.SWEEP.value)
        self.method.addItem("Dirac impulse", Method.DIRAC.value)
        self.rate = QComboBox()
        for r in SAMPLE_RATES:
            self.rate.addItem(f"{r / 1000:g} kHz", r)
        self.length = _spin(0.001, 30.0, 3, 0.05, " s")
        self.length_hint = QLabel()
        self.length_hint.setWordWrap(True)
        self.length_hint.setStyleSheet("color: gray")
        self.length_msg = MessageLabel()
        self.stereo = QComboBox()
        self.ts_out = QComboBox()
        self.ts_out.addItem("Two stereo files (_ir_inL / _ir_inR) — for IRene", TrueStereoOutput.TWO_FILES.value)
        self.ts_out.addItem("One 4-channel WAV (LL, LR, RL, RR)", TrueStereoOutput.FOUR_CHANNEL.value)
        self.ts_label = QLabel("True stereo output:")
        self.out_dir = QLineEdit()
        browse = QPushButton("Browse…")
        browse.clicked.connect(self._browse_out)
        out_row = QHBoxLayout()
        out_row.addWidget(self.out_dir, 1)
        out_row.addWidget(browse)

        # Advanced
        self.sweep_len = _spin(1.0, 30.0, 1, 1.0, " s")
        self.f_start = _spin(1.0, 1000.0, 1, 5.0, " Hz")
        self.f_end = _spin(1000.0, 95000.0, 0, 500.0, " Hz")
        self.f_end_auto = QCheckBox("0.95 × Nyquist")
        self.f_end_auto.toggled.connect(lambda on: self.f_end.setEnabled(not on))
        f_end_row = QHBoxLayout()
        f_end_row.addWidget(self.f_end)
        f_end_row.addWidget(self.f_end_auto)
        self.level = _spin(-24.0, 0.0, 1, 1.0, " dBFS")
        self.test_bits = QComboBox()
        self.test_bits.addItem("32-bit float (recommended)", BitDepth.FLOAT32.value)
        self.test_bits.addItem("24-bit", BitDepth.PCM24.value)
        self.ir_bits = QComboBox()
        self.ir_bits.addItem("24-bit", BitDepth.PCM24.value)
        self.ir_bits.addItem("32-bit float", BitDepth.FLOAT32.value)
        self.trim = QCheckBox("Trim latency (remove silence before the first sound)")
        self.tail = QCheckBox("Automatic tail trim: end the IR where it has died away (with a fade-out)")
        self.tail.setToolTip("The IR can then be shorter than the IR length. Off: the IR has exactly the IR length.")
        self.norm = QComboBox()
        self.norm.addItem("Off (absolute gain)", Normalization.OFF.value)
        self.norm.addItem("Peak", Normalization.PEAK.value)
        self.norm.addItem("Energy (unit energy)", Normalization.ENERGY.value)
        self.peak = _spin(-30.0, 0.0, 1, 0.5, " dBFS")
        norm_row = QHBoxLayout()
        norm_row.addWidget(self.norm)
        norm_row.addWidget(QLabel("target"))
        norm_row.addWidget(self.peak)
        norm_row.addStretch()

        adv = QWidget()
        af = QFormLayout(adv)
        af.addRow("Sweep length:", self.sweep_len)
        af.addRow("Start frequency:", self.f_start)
        af.addRow("End frequency:", f_end_row)
        af.addRow("Test level (peak):", self.level)
        af.addRow("Test file format:", self.test_bits)
        af.addRow("IR file format:", self.ir_bits)
        af.addRow("", self.trim)
        af.addRow("", self.tail)
        af.addRow("Normalization:", norm_row)

        form = QFormLayout()
        form.addRow("Capture type:", self.type)
        form.addRow("Method:", self.method)
        form.addRow("Sample rate:", self.rate)
        form.addRow("IR length (max.):", self.length)
        form.addRow("", self.length_hint)
        form.addRow("", self.length_msg)
        form.addRow("Channels:", self.stereo)
        form.addRow(self.ts_label, self.ts_out)
        form.addRow("Output folder:", out_row)

        intro = QLabel(
            "<b>Only linear, time-invariant (LTI) processing can be captured as an IR</b>: cabinet sims, "
            "reverbs, EQs and filters. Amps, drive, compressors and modulation effects give poor results — "
            "irene-capture detects that and tells you.")
        intro.setWordWrap(True)
        self.messages = MessageLabel()
        nxt = QPushButton("Next: Capture →")
        nxt.setDefault(True)
        nxt.clicked.connect(self.next_requested)
        reset = QPushButton("Reset to preset defaults")
        reset.clicked.connect(lambda: self._apply_preset(self.capture_type()))

        inner = QWidget()
        v = QVBoxLayout(inner)
        v.addWidget(intro)
        v.addLayout(form)
        v.addWidget(CollapsibleSection("Advanced", adv, expanded=False))
        v.addWidget(self.messages)
        v.addStretch()
        row = QHBoxLayout()
        row.addWidget(reset)
        row.addStretch()
        row.addWidget(nxt)
        v.addLayout(row)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(inner)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(scroll)

        self.type.currentIndexChanged.connect(lambda _: self._apply_preset(self.capture_type()))
        for w in (self.method, self.rate, self.stereo, self.ts_out, self.test_bits, self.ir_bits, self.norm):
            w.currentIndexChanged.connect(self._on_change)
        for w in (self.length, self.sweep_len, self.f_start, self.f_end, self.level, self.peak):
            w.valueChanged.connect(self._on_change)
        for w in (self.trim, self.tail, self.f_end_auto):
            w.toggled.connect(self._on_change)
        self.out_dir.textChanged.connect(self._on_change)
        self._restore()

    # --- values -------------------------------------------------------------------------
    def capture_type(self) -> CaptureType:
        return CaptureType(self.type.currentData())

    def output_folder(self) -> Path:
        return Path(self.out_dir.text()).expanduser() if self.out_dir.text().strip() else Path.home()

    def settings(self) -> CaptureSettings:
        return CaptureSettings(
            capture_type=self.capture_type(),
            method=Method(self.method.currentData()),
            sample_rate=int(self.rate.currentData()),
            ir_length_s=self.length.value(),
            stereo_mode=StereoMode(self.stereo.currentData() or StereoMode.MONO.value),
            true_stereo_output=TrueStereoOutput(self.ts_out.currentData()),
            sweep_length_s=self.sweep_len.value(),
            f_start_hz=self.f_start.value(),
            f_end_hz=None if self.f_end_auto.isChecked() else self.f_end.value(),
            test_level_dbfs=self.level.value(),
            test_bit_depth=BitDepth(self.test_bits.currentData()),
            ir_bit_depth=BitDepth(self.ir_bits.currentData()),
            trim_latency=self.trim.isChecked(),
            tail_trim=self.tail.isChecked(),
            normalization=Normalization(self.norm.currentData()),
            peak_target_dbfs=self.peak.value(),
        )

    def apply(self, s: CaptureSettings) -> None:
        self._loading = True
        try:
            self.type.setCurrentIndex(self.type.findData(s.capture_type.value))
            self._fill_stereo(s.capture_type)
            self.method.setCurrentIndex(self.method.findData(s.method.value))
            self.rate.setCurrentIndex(max(0, self.rate.findData(s.sample_rate)))
            self.length.setValue(s.ir_length_s)
            self.stereo.setCurrentIndex(max(0, self.stereo.findData(s.stereo_mode.value)))
            self.ts_out.setCurrentIndex(self.ts_out.findData(s.true_stereo_output.value))
            self.sweep_len.setValue(s.sweep_length_s)
            self.f_start.setValue(s.f_start_hz)
            self.f_end_auto.setChecked(s.f_end_hz is None)
            self.f_end.setValue(s.f_end_hz or s.effective_f_end_hz)
            self.level.setValue(s.test_level_dbfs)
            self.test_bits.setCurrentIndex(self.test_bits.findData(s.test_bit_depth.value))
            self.ir_bits.setCurrentIndex(self.ir_bits.findData(s.ir_bit_depth.value))
            self.trim.setChecked(s.trim_latency)
            self.tail.setChecked(s.tail_trim)
            self.norm.setCurrentIndex(self.norm.findData(s.normalization.value))
            self.peak.setValue(s.peak_target_dbfs)
        finally:
            self._loading = False
        self._on_change()

    def _fill_stereo(self, t: CaptureType) -> None:
        current = self.stereo.currentData()
        self.stereo.blockSignals(True)
        self.stereo.clear()
        for m in PRESETS[t].stereo_modes:
            self.stereo.addItem(STEREO_LABELS[m], m.value)
        idx = self.stereo.findData(current)
        self.stereo.setCurrentIndex(idx if idx >= 0 else 0)
        self.stereo.setEnabled(self.stereo.count() > 1)
        self.stereo.blockSignals(False)

    def _apply_preset(self, t: CaptureType) -> None:
        if self._loading:
            return
        cur = self.settings()
        new = CaptureSettings.for_type(t, method=cur.method, sample_rate=cur.sample_rate,
                                       sweep_length_s=cur.sweep_length_s, f_start_hz=cur.f_start_hz,
                                       f_end_hz=cur.f_end_hz, test_level_dbfs=cur.test_level_dbfs,
                                       test_bit_depth=cur.test_bit_depth, ir_bit_depth=cur.ir_bit_depth,
                                       true_stereo_output=cur.true_stereo_output)
        self.apply(new)

    def _on_change(self, *_):
        if self._loading:
            return
        s = self.settings()
        self.peak.setEnabled(s.normalization == Normalization.PEAK)
        ts = s.stereo_mode == StereoMode.TRUE_STEREO
        self.ts_out.setVisible(ts)
        self.ts_label.setVisible(ts)
        self.sweep_len.setEnabled(s.method == Method.SWEEP)
        self.length_hint.setText(
            "The IR may end earlier: automatic tail trim is on (Advanced)." if s.tail_trim else
            "The IR will have exactly this length (automatic tail trim is off).")
        msgs = validate(s)
        self.length_msg.set_messages([m for m in msgs if m.field == "ir_length_s"])
        self.messages.set_messages([m for m in msgs if m.field != "ir_length_s"])
        self._save()
        self.changed.emit()

    def has_errors(self) -> bool:
        return any(m.severity == "error" for m in validate(self.settings()))

    def _browse_out(self) -> None:
        d = QFileDialog.getExistingDirectory(self, "Output folder", str(self.output_folder()))
        if d:
            self.out_dir.setText(d)

    # --- persistence --------------------------------------------------------------------
    def _save(self) -> None:
        self.qs.setValue("setup/settings", json.dumps(self.settings().to_dict()))
        self.qs.setValue("setup/version", SETTINGS_VERSION)
        self.qs.setValue("paths/output", self.out_dir.text())

    def _restore(self) -> None:
        raw = self.qs.value("setup/settings", "")
        try:
            s = CaptureSettings.from_dict(json.loads(raw)) if raw else CaptureSettings.for_type("cabinet")
        except (ValueError, TypeError, KeyError):
            s = CaptureSettings.for_type("cabinet")
        if int(self.qs.value("setup/version", 1) or 1) < 2 and s.capture_type == CaptureType.CABINET:
            s.tail_trim = False  # v0.1.0 had tail trim on for cabinets; the new default is off
        self.out_dir.setText(str(self.qs.value("paths/output", "") or (Path.home() / "irene-capture")))
        self.apply(s)
