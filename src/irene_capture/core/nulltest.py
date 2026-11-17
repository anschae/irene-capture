"""Null test: predict the rendered verification segment from the IR and compare.

The verification signal (band-limited pink noise) is not used to compute the IR, so the
residual shows how well the IR describes the plugin on independent material.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import scipy.fft as sfft
from scipy.signal import butter, fftconvolve, sosfiltfilt, welch

from .checks import db, rms
from .signals import band_weight

RATINGS = (
    (-60.0, "excellent"),
    (-40.0, "very good"),
    (-25.0, "audible differences possible"),
)
UNUSABLE = "unusable"
BANDS = (("< 200 Hz", None, 200.0), ("200 Hz – 5 kHz", 200.0, 5000.0), ("> 5 kHz", 5000.0, None))


def rating_for(residual_db: float) -> str:
    for limit, name in RATINGS:
        if residual_db <= limit:
            return name
    return UNUSABLE


@dataclass
class NullTestResult:
    residual_db: float  # all channels together
    per_channel_db: list[float]
    channel_names: list[str]
    bands_db: dict[str, float] = field(default_factory=dict)
    rating: str = ""
    spectrum_freqs: np.ndarray | None = None
    spectrum_error_db: np.ndarray | None = None  # residual PSD re rendered PSD

    def to_dict(self) -> dict:
        return {
            "residual_db": round(self.residual_db, 2),
            "rating": self.rating,
            "per_channel_db": {n: round(v, 2) for n, v in zip(self.channel_names, self.per_channel_db)},
            "bands_db": {k: round(v, 2) for k, v in self.bands_db.items()},
        }


def predict(dry: np.ndarray, ir: np.ndarray, ir_start: int, length: int) -> np.ndarray:
    """Plugin output predicted from the IR.

    ``dry``: (n,) input starting at segment time 0. ``ir``: (outputs, L), whose first sample
    is time ``ir_start`` relative to zero delay. Returns (length, outputs) for segment times
    [0, length).
    """
    out = np.zeros((length, ir.shape[0]))
    for c in range(ir.shape[0]):
        y = fftconvolve(dry, ir[c])
        # y[i] is time i + ir_start
        a = ir_start
        lo, hi = max(0, a), min(length, a + y.size)
        if hi > lo:
            out[lo:hi, c] = y[lo - a:hi - a]
    return out


def _band(x: np.ndarray, sr: int, lo: float | None, hi: float | None) -> np.ndarray:
    nyq = sr / 2
    if lo is None:
        sos = butter(4, hi / nyq, "lowpass", output="sos")
    elif hi is None:
        sos = butter(4, lo / nyq, "highpass", output="sos")
    else:
        sos = butter(4, [lo / nyq, hi / nyq], "bandpass", output="sos")
    return sosfiltfilt(sos, x, axis=0)


def band_limit(x: np.ndarray, sample_rate: int, f_lo: float, f_hi: float) -> np.ndarray:
    """Zero-phase FFT band-limit of (n, channels) to the band of the verification noise."""
    n = x.shape[0]
    m = sfft.next_fast_len(2 * n, real=True)
    f = sfft.rfftfreq(m, 1.0 / sample_rate)
    w = np.zeros_like(f)
    w[1:] = band_weight(f[1:], f_lo, f_hi)
    return sfft.irfft(sfft.rfft(x, m, axis=0) * w[:, None], m, axis=0)[:n]


def compare(predicted: np.ndarray, rendered: np.ndarray, sample_rate: int, channel_names: list[str],
            with_details: bool = True, band: tuple[float, float] | None = None) -> NullTestResult:
    """Residual RMS re rendered RMS, in dB (per channel, overall, per band).

    With ``band``, both signals are first limited to that band: the IR is only defined
    inside the sweep band, and the verification noise lives there too.
    """
    if band is not None:
        predicted = band_limit(predicted, sample_rate, *band)
        rendered = band_limit(rendered, sample_rate, *band)
    residual = rendered - predicted
    per = [db(rms(residual[:, c]) / max(rms(rendered[:, c]), 1e-300)) for c in range(rendered.shape[1])]
    total = db(rms(residual) / max(rms(rendered), 1e-300))
    res = NullTestResult(total, per, channel_names, rating=rating_for(total))
    if with_details and rendered.shape[0] > 1024:
        for name, lo, hi in BANDS:
            if hi is not None and hi >= sample_rate / 2:
                continue
            r = _band(residual, sample_rate, lo, hi)
            s = _band(rendered, sample_rate, lo, hi)
            res.bands_db[name] = db(rms(r) / max(rms(s), 1e-300))
        nper = min(8192, rendered.shape[0])
        f, pr = welch(residual, sample_rate, nperseg=nper, axis=0)
        _, ps = welch(rendered, sample_rate, nperseg=nper, axis=0)
        pr, ps = pr.sum(axis=1), ps.sum(axis=1)
        with np.errstate(divide="ignore", invalid="ignore"):
            err = 10 * np.log10(np.maximum(pr, 1e-30) / np.maximum(ps, 1e-30))
        res.spectrum_freqs, res.spectrum_error_db = f, err
    return res
