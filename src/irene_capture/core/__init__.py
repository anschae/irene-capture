"""Pure DSP core: no GUI, no audio devices.

The public API is small:

- :func:`create_session` builds a :class:`Session` from :class:`CaptureSettings`.
- :func:`render_test_signal` returns the exact test signal for a session.
- :func:`analyze` turns a rendered file into a :class:`CaptureResult`.
- :func:`write_outputs` writes the IR and the reports.
"""

from .analysis import AnalysisOptions, NeedsDecision, analyze
from .audio_io import RenderedAudio, read_audio
from .checks import CheckResult, Severity
from .presets import PRESETS, CaptureSettings, CaptureType, Method, Normalization, StereoMode
from .report import write_outputs
from .result import CaptureResult
from .session import Session, create_session, load_session
from .signals import render_test_signal

__all__ = [
    "PRESETS",
    "AnalysisOptions",
    "CaptureResult",
    "CaptureSettings",
    "CaptureType",
    "CheckResult",
    "Method",
    "NeedsDecision",
    "Normalization",
    "RenderedAudio",
    "Session",
    "Severity",
    "StereoMode",
    "analyze",
    "create_session",
    "load_session",
    "read_audio",
    "render_test_signal",
    "write_outputs",
]
