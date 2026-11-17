"""Post-processing of the raw IR: latency trim, tail detection, fades, normalization.

All functions work on (channels, samples) arrays and apply the same decision to every
channel, so the timing between channels is preserved.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
from scipy.ndimage import gaussian_filter1d, uniform_filter1d

from .presets import Normalization

ONSET_THRESHOLD_DB = -40.0
ONSET_SMOOTH_SIGMA = 1.5  # samples; removes ringing near Nyquist before onset detection (≈ −87 dB at 0.95·Nyquist)
PRE_ONSET_MARGIN_S = 0.0005
NEGATIVE_CONTENT_GUARD = 8  # samples; onsets later than −8 count as "starts at zero"
NEGATIVE_CONTENT_DB = -80.0
NEGATIVE_CONTENT_GAP_S = 0.002
ENVELOPE_WINDOW_S = 0.010
FLOAT_FLOOR_DB = -100.0
TRUNCATION_LEVEL_DB = -60.0
TAIL_MARGIN_S = 0.020


def onset_detector(h: np.ndarray) -> np.ndarray:
    """Max over channels of |h| after a light Gaussian low-pass."""
    sm = gaussian_filter1d(h, ONSET_SMOOTH_SIGMA, axis=-1, mode="constant")
    return np.max(np.abs(sm), axis=0)


def find_onset(h: np.ndarray, threshold_db: float = ONSET_THRESHOLD_DB) -> int:
    """Index of the first sample above ``threshold_db`` relative to the peak."""
    d = onset_detector(h)
    peak = d.max()
    if peak <= 0:
        return 0
    return int(np.argmax(d >= peak * 10 ** (threshold_db / 20)))


def find_early_onset(h: np.ndarray, onset: int, threshold_db: float, gap: int) -> int:
    """Walk back from ``onset`` while content above ``threshold_db`` (re peak) continues.

    Content separated from the main onset by more than ``gap`` samples below the threshold
    (isolated junk, distortion products) is ignored. Returns an index into ``h``.
    """
    d = onset_detector(h)
    peak = d.max()
    if peak <= 0:
        return onset
    above = d >= peak * 10 ** (threshold_db / 20)
    i = onset
    quiet = 0
    earliest = onset
    while i > 0 and quiet <= gap:
        i -= 1
        if above[i]:
            earliest = i
            quiet = 0
        else:
            quiet += 1
    return earliest


def envelope_db(h: np.ndarray, sample_rate: int, window_s: float = ENVELOPE_WINDOW_S) -> np.ndarray:
    """Smoothed energy envelope (mean over channels), dB re its maximum."""
    p = np.mean(h**2, axis=0)
    win = max(1, int(round(window_s * sample_rate)))
    env = uniform_filter1d(p, win, mode="constant")
    m = env.max()
    if m <= 0:
        return np.full(p.shape, -300.0)
    with np.errstate(divide="ignore"):
        return 10 * np.log10(np.maximum(env / m, 1e-30))


def envelope_peak_power(h: np.ndarray, sample_rate: int, window_s: float = ENVELOPE_WINDOW_S) -> float:
    p = np.mean(h**2, axis=0)
    win = max(1, int(round(window_s * sample_rate)))
    return float(uniform_filter1d(p, win, mode="constant").max())


def half_hann_fade(h: np.ndarray, fade_len: int) -> np.ndarray:
    h = h.copy()
    fade_len = min(fade_len, h.shape[1])
    if fade_len > 0:
        k = np.arange(fade_len)
        h[:, h.shape[1] - fade_len:] *= 0.5 + 0.5 * np.cos(np.pi * (k + 0.5) / fade_len)
    return h


@dataclass
class TailInfo:
    length: int  # samples kept
    threshold_db: float  # dB re envelope peak
    noise_floor_db: float  # dB re envelope peak (-300 = none measurable)
    end_level_db: float  # level at the end of the full IR window, dB re envelope peak
    truncated: bool
    fade_len: int


def analyze_tail(h: np.ndarray, sample_rate: int, noise_power: float, trim: bool) -> TailInfo:
    """Decide where the IR ends.

    ``h`` is the IR from its start to the maximum allowed length. ``noise_power`` is the
    IR-domain noise power per sample (0 if none). The decay is considered finished where
    the envelope reaches the noise floor + 6 dB, or −100 dB re its peak (float renders).
    """
    n = h.shape[1]
    env = envelope_db(h, sample_rate)
    peak_power = envelope_peak_power(h, sample_rate)
    noise_db = 10 * np.log10(noise_power / peak_power) if noise_power > 0 and peak_power > 0 else -300.0
    threshold = max(noise_db + 6.0, FLOAT_FLOOR_DB)
    win = max(1, int(round(0.02 * sample_rate)))
    end_level = float(np.max(env[-win:])) if n else -300.0
    truncated = end_level > max(noise_db + 10.0, TRUNCATION_LEVEL_DB)
    if not trim:
        return TailInfo(n, threshold, noise_db, end_level, truncated, 0)
    above = np.flatnonzero(env > threshold)
    last = int(above[-1]) if above.size else 0
    length = min(n, last + int(round(TAIL_MARGIN_S * sample_rate)))
    length = max(length, min(n, int(round(0.002 * sample_rate))))
    fade = int(np.clip(0.1 * length, 0.005 * sample_rate, 0.2 * sample_rate))
    fade = min(fade, length)
    return TailInfo(length, threshold, noise_db, end_level, truncated, fade)


def normalization_gain(h: np.ndarray, mode: Normalization, peak_target_dbfs: float) -> float:
    """Linear gain. Energy: mean energy per IR channel = 1. Peak: max |h| = target."""
    if mode == Normalization.PEAK:
        peak = float(np.max(np.abs(h)))
        return 10 ** (peak_target_dbfs / 20) / peak if peak > 0 else 1.0
    if mode == Normalization.ENERGY:
        e = float(np.sum(h**2)) / h.shape[0]
        return 1.0 / np.sqrt(e) if e > 0 else 1.0
    return 1.0
