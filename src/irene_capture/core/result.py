"""The result of one analysis."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from .checks import CheckResult, Severity, worst
from .deconvolution import DistortionEstimate
from .nulltest import NullTestResult
from .postprocess import TailInfo
from .presets import BitDepth, Normalization
from .session import Session


@dataclass
class CaptureResult:
    session: Session
    source_name: str
    source_path: Path | None
    checks: list[CheckResult] = field(default_factory=list)
    ir: np.ndarray | None = None  # (inputs, outputs, samples), final (normalized) IR
    channel_names: list[str] = field(default_factory=list)
    ir_start: int = 0  # time of the IR's first sample relative to zero delay (samples)
    offset_samples: int = 0  # coarse offset found by cross-correlation
    realigned_samples: int = 0  # >0: leading samples removed, <0: silence added
    normalization: Normalization = Normalization.OFF
    normalization_gain_db: float = 0.0
    trim_latency: bool = False
    tail_trim: bool = False
    ir_bit_depth: BitDepth = BitDepth.PCM24
    tail: TailInfo | None = None
    distortion: DistortionEstimate | None = None
    null_test: NullTestResult | None = None
    capture_null: NullTestResult | None = None
    rt60_s: float | None = None
    noise_floor_dbfs: float | None = None  # in the rendered pre-roll
    hints: list[str] = field(default_factory=list)
    plugin_info: dict = field(default_factory=dict)

    @property
    def sample_rate(self) -> int:
        return self.session.sample_rate

    @property
    def severity(self) -> Severity:
        return worst(self.checks)

    @property
    def ok(self) -> bool:
        """True if the IR can be written (no errors)."""
        return self.ir is not None and self.severity != Severity.ERROR

    @property
    def conversions(self) -> list[CheckResult]:
        return [c for c in self.checks if c.conversion]

    @property
    def latency_trimmed_samples(self) -> int:
        return max(self.ir_start, 0)

    @property
    def length_samples(self) -> int:
        return 0 if self.ir is None else int(self.ir.shape[2])

    @property
    def rating(self) -> str:
        return self.null_test.rating if self.null_test else "not tested"

    def ir_channels(self) -> np.ndarray:
        """(channels, samples) in output-file order: mono | L, R | LL, LR, RL, RR."""
        assert self.ir is not None
        return self.ir.reshape(-1, self.ir.shape[2])
