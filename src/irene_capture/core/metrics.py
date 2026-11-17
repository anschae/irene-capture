"""Display metrics: magnitude response with fractional-octave smoothing, energy decay, RT60."""

from __future__ import annotations

import numpy as np
import scipy.fft as sfft


def magnitude_response(ir: np.ndarray, sample_rate: int, smoothing_octaves: float | None = None,
                       n_points: int = 1024, f_min: float = 10.0) -> tuple[np.ndarray, np.ndarray]:
    """Log-spaced frequencies and magnitude in dB for a (L,) IR."""
    n = sfft.next_fast_len(max(ir.size, 65536), real=True)
    spec = np.abs(sfft.rfft(ir, n)) ** 2
    f = sfft.rfftfreq(n, 1.0 / sample_rate)
    grid = np.geomspace(f_min, sample_rate / 2 * 0.999, n_points)
    if smoothing_octaves:
        half = 2 ** (smoothing_octaves / 2)
        cs = np.concatenate([[0.0], np.cumsum(spec)])
        lo = np.searchsorted(f, grid / half)
        hi = np.maximum(np.searchsorted(f, grid * half), lo + 1)
        vals = (cs[hi] - cs[lo]) / (hi - lo)
    else:
        vals = np.interp(grid, f, spec)
    return grid, 10 * np.log10(np.maximum(vals, 1e-30))


def energy_decay_curve(ir: np.ndarray) -> np.ndarray:
    """Schroeder backward integration, dB re total energy. ``ir`` (channels, L) or (L,)."""
    e = np.atleast_2d(ir) ** 2
    e = e.mean(axis=0)
    edc = np.cumsum(e[::-1])[::-1]
    return 10 * np.log10(np.maximum(edc / max(edc[0], 1e-300), 1e-30))


def rt60(ir: np.ndarray, sample_rate: int) -> float | None:
    """RT60 from a T30 fit (−5 … −35 dB) of the EDC, or T20 if the range is too small."""
    edc = energy_decay_curve(ir)
    t = np.arange(edc.size) / sample_rate
    for lo, hi in ((-5.0, -35.0), (-5.0, -25.0)):
        sel = (edc <= lo) & (edc >= hi)
        if sel.sum() < 10 or edc.min() > hi:
            continue
        slope, _ = np.polyfit(t[sel], edc[sel], 1)
        if slope >= 0:
            continue
        return float(-60.0 / slope)
    return None
