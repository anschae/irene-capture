"""Import checks.

Every check produces a :class:`CheckResult` with a severity and a plain-language message
that tells the user what to do. The checks that need the audio are small pure functions
so they can be tested one by one; :mod:`analysis` calls them in order.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import StrEnum

import numpy as np


class Severity(StrEnum):
    OK = "ok"
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"

    @property
    def rank(self) -> int:
        return {"ok": 0, "info": 1, "warning": 2, "error": 3}[self.value]


@dataclass(frozen=True)
class Decision:
    """A question the user must answer before the analysis can continue."""

    key: str
    question: str
    choices: tuple[tuple[str, str], ...]  # (value, label)
    default: str


@dataclass
class CheckResult:
    id: str
    severity: Severity
    message: str
    detail: str = ""
    conversion: bool = False  # True if the app changed the audio (shown under "Conversions")
    decision: Decision | None = None
    data: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        d = asdict(self)
        d["severity"] = self.severity.value
        if self.decision is None:
            d.pop("decision")
        return d


def worst(checks: list[CheckResult]) -> Severity:
    if not checks:
        return Severity.OK
    return max((c.severity for c in checks), key=lambda s: s.rank)


def db(x: float, floor: float = -300.0) -> float:
    return float(20 * np.log10(x)) if x > 0 else floor


def rms(x: np.ndarray) -> float:
    return float(np.sqrt(np.mean(np.square(x, dtype=np.float64)))) if x.size else 0.0


# --- individual checks -------------------------------------------------------------------


def check_sample_rate(file_rate: int, session_rate: int) -> CheckResult:
    if file_rate == session_rate:
        return CheckResult("sample_rate", Severity.OK, f"Sample rate matches the session ({session_rate / 1000:g} kHz).")
    return CheckResult(
        "sample_rate",
        Severity.ERROR,
        f"The rendered file is {file_rate / 1000:g} kHz, but the test file was {session_rate / 1000:g} kHz.",
        "irene-capture does not resample, because resampling would change the IR. "
        f"Set the DAW project to {session_rate / 1000:g} kHz before importing the test file "
        "(Cubase: Project → Project Setup → Sample Rate; Logic: File → Project Settings → Audio → Sample Rate), "
        "then render again. If the DAW offered to convert the test file on import, decline.",
    )


def check_bit_depth(subtype: str) -> CheckResult:
    s = subtype.upper()
    if s in ("FLOAT", "DOUBLE"):
        return CheckResult("bit_depth", Severity.OK, f"File is {'32' if s == 'FLOAT' else '64'}-bit float.")
    if s == "PCM_16":
        return CheckResult(
            "bit_depth", Severity.WARNING,
            "File is 16-bit: reduced quality (noise floor about −96 dBFS).",
            "Converted to float for the analysis. Export as 32-bit float (or at least 24-bit) for a cleaner IR.",
            conversion=True)
    if s in ("PCM_24", "PCM_32"):
        bits = s[-2:]
        return CheckResult("bit_depth", Severity.INFO, f"{bits}-bit integer file converted to float for the analysis.",
                           "This is lossless. 32-bit float is still recommended, because it can't clip.",
                           conversion=True)
    return CheckResult("bit_depth", Severity.WARNING, f"Unusual sample format ({subtype}) converted to float.",
                       "Export as WAV, 32-bit float, if possible.", conversion=True)


def check_silent(audio: np.ndarray) -> CheckResult:
    peak = float(np.max(np.abs(audio))) if audio.size else 0.0
    if db(peak) < -90.0:
        return CheckResult(
            "silent", Severity.ERROR, "No output: the rendered file is silent.",
            f"The peak level is {db(peak):.0f} dBFS. Was the wrong track exported, or is the plugin or track muted? "
            "Check that the track is not muted, its fader is up and the export range covers the test file.")
    return CheckResult("silent", Severity.OK, f"Output level OK (peak {db(peak):.1f} dBFS).")


def full_scale_runs(x: np.ndarray, threshold: float, min_run: int = 3) -> int:
    """Number of runs of at least ``min_run`` consecutive samples with |x| >= threshold."""
    count = 0
    for ch in np.atleast_2d(x.T):
        hot = np.abs(ch) >= threshold
        if not hot.any():
            continue
        padded = np.concatenate(([False], hot, [False])).astype(np.int8)
        d = np.diff(padded)
        starts, ends = np.flatnonzero(d == 1), np.flatnonzero(d == -1)
        count += int(np.sum((ends - starts) >= min_run))
    return count


def check_clipping(audio: np.ndarray, is_float: bool, bits: int | None) -> CheckResult:
    if is_float:
        peak = float(np.max(np.abs(audio))) if audio.size else 0.0
        if peak > 1.0:
            return CheckResult(
                "clipping", Severity.WARNING, f"Output goes above 0 dBFS (peak {db(peak):+.1f} dBFS).",
                "The IR is fine because the file is float, but this plugin setting would clip in a fixed-point "
                "path. If you want headroom, lower the test level in Advanced settings.")
        return CheckResult("clipping", Severity.OK, "No clipping.")
    full = (2 ** ((bits or 24) - 1) - 1) / 2 ** ((bits or 24) - 1)
    runs = full_scale_runs(audio, full - 1e-9)
    if runs:
        return CheckResult(
            "clipping", Severity.ERROR, f"The rendered file is clipped ({runs} run(s) of full-scale samples).",
            "Clipping is non-linear and ruins the IR. Lower the test level in Advanced settings (e.g. to −12 dBFS), "
            "or export as 32-bit float, and render again.")
    return CheckResult("clipping", Severity.OK, "No clipping.")


def channels_identical(audio: np.ndarray, tol_db: float = -80.0) -> bool:
    if audio.shape[1] < 2:
        return True
    left, right = audio[:, 0], audio[:, 1]
    ref = max(rms(left), rms(right))
    if ref == 0:
        return True
    return db(rms(left - right) / ref) < tol_db
