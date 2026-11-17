import os

import numpy as np
import pytest

from irene_capture.core import AnalysisOptions, CaptureSettings, analyze, create_session, render_test_signal
from irene_capture.core.signals import band_weight, verify_top
from irene_capture.testing import simulate_render


@pytest.fixture(autouse=True)
def _isolated_config(tmp_path, monkeypatch):
    monkeypatch.setenv("IRENE_CAPTURE_CONFIG_DIR", str(tmp_path / "config"))


def capture(capture_type, plugin, settings=None, options=None, **render):
    """Create a session, simulate a render through ``plugin`` and analyze it."""
    s = create_session(CaptureSettings.for_type(capture_type, **(settings or {})))
    x = render_test_signal(s)
    r = simulate_render(plugin, x, s.sample_rate, **render)
    res = analyze(s, r, options or AnalysisOptions())
    return s, res


def raw_options(**decisions):
    """No trim, no tail trim, no normalization: the IR as measured."""
    from irene_capture.core.presets import Normalization

    return AnalysisOptions(decisions=decisions, trim_latency=False, tail_trim=False,
                           normalization=Normalization.OFF)


def band_error_db(recovered: np.ndarray, true: np.ndarray, sr: int, f_lo: float, f_hi: float) -> float:
    """Error of ``recovered`` vs ``true`` (same time origin), both limited to the trusted band."""
    n = max(recovered.size, true.size)
    m = 1 << int(np.ceil(np.log2(4 * n)))
    f = np.fft.rfftfreq(m, 1 / sr)
    w = np.zeros_like(f)
    w[1:] = band_weight(f[1:], f_lo, verify_top(f_hi))
    a = np.fft.irfft(np.fft.rfft(recovered, m) * w, m)
    b = np.fft.irfft(np.fft.rfft(true, m) * w, m)
    return float(20 * np.log10(np.sqrt(np.sum((a - b) ** 2)) / np.sqrt(np.sum(b**2))))


def pad(h: np.ndarray, n: int) -> np.ndarray:
    out = np.zeros(n)
    out[: min(n, h.size)] = h[:n]
    return out


def has_display() -> bool:
    return bool(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))
