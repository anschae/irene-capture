"""Turn a rendered capture segment into a raw (two-sided) impulse response.

Sweep: Wiener-regularized spectral division ``H = Y·X* / (|X|² + |N|²)`` with linear
(zero-padded) FFTs, where ``|N|²`` is the noise of the recording (measured in the pre-roll,
plus the quantization noise of the file format). The result is exact wherever the sweep is
well above the noise, and noise is not amplified where the sweep has no energy. Harmonic distortion products land at
negative time (``-L·ln k``) and are excluded from the IR window.

Dirac: the rendered segment from the impulse position divided by the impulse amplitude.

A raw IR is returned as a two-sided array: ``data[:, zero + t]`` is time ``t`` (samples,
may be negative) relative to the zero-delay position.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import scipy.fft as sfft

EPS_FLOOR = 1e-12  # relative to the in-band maximum of |X|², only for numerical safety


@dataclass
class RawIR:
    data: np.ndarray  # (channels, n)
    zero: int  # index of t = 0

    @property
    def neg(self) -> int:
        return self.zero

    @property
    def pos(self) -> int:
        return self.data.shape[1] - self.zero

    def window(self, start: int, length: int) -> np.ndarray:
        """Samples for times [start, start + length), zero where not available."""
        out = np.zeros((self.data.shape[0], length))
        a, b = start + self.zero, start + self.zero + length
        sa, sb = max(a, 0), min(b, self.data.shape[1])
        if sb > sa:
            out[:, sa - a:sb - a] = self.data[:, sa:sb]
        return out


@dataclass
class SweepDeconvolution:
    """Keeps the full circular result for the distortion analysis."""

    full: np.ndarray  # (channels, N) circular IR, negative times wrapped to the end
    n_fft: int
    noise_gain: np.ndarray  # per channel: IR-domain noise RMS per unit RMS of white noise in the recording

    def raw(self, neg: int, pos: int) -> RawIR:
        data = np.concatenate([self.full[:, self.n_fft - neg:], self.full[:, :pos]], axis=1) if neg else \
            self.full[:, :pos].copy()
        return RawIR(data, neg)

    def circular(self, start: int, length: int) -> np.ndarray:
        idx = np.arange(start, start + length) % self.n_fft
        return self.full[:, idx]


TOP_MARGIN_OCTAVES = 1 / 12  # the last bit of the sweep (fade-out) is not trusted
HF_EPS = 1e-3  # extra regularization above the sweep, re the in-band maximum of |X|²


def trusted_top(f_hi: float) -> float:
    """Upper edge of the band in which the IR is known exactly (a bit below the sweep's end)."""
    return f_hi * 2 ** -TOP_MARGIN_OCTAVES


def hf_taper(freqs: np.ndarray, f_top: float, nyquist: float) -> np.ndarray:
    """1 up to f_top, raised cosine down to 0 at Nyquist (zero phase).

    Above the sweep band there is no signal, so the IR can't be known there. A smooth
    taper keeps the resulting ringing short instead of a long ring at Nyquist.
    """
    w = np.ones_like(freqs)
    if f_top < nyquist:
        sel = freqs > f_top
        w[sel] = 0.5 + 0.5 * np.cos(np.pi * np.clip((freqs[sel] - f_top) / (nyquist - f_top), 0, 1))
    return w


def _raised(freqs: np.ndarray, f0: float, f1: float) -> np.ndarray:
    """0 below f0, 1 above f1, raised cosine in between."""
    x = np.clip((freqs - f0) / max(f1 - f0, 1e-9), 0, 1)
    return 0.5 - 0.5 * np.cos(np.pi * x)


def hf_continuation(H: np.ndarray, freqs: np.ndarray, sample_rate: int, f_top: float) -> np.ndarray | None:
    """Continue H above ``f_top`` with constant magnitude and linear phase, if H looks like a delay there.

    Plugins that are flat at the top (gain, delay, many EQs) then get an exact IR without
    the pre-ringing a band limit would cause. Returns None if H isn't coherent enough.
    """
    fit = (freqs >= f_top * 2 ** (-1 / 3)) & (freqs <= f_top * 2 ** (-1 / 12))
    if fit.sum() < 32:
        return None
    hf = H[fit]
    mag = np.abs(hf)
    if mag.min() <= 0:
        return None
    mag_db = 20 * np.log10(mag)
    if np.std(mag_db) > 1.0:
        return None
    w = 2 * np.pi * freqs[fit] / sample_rate
    phase = np.unwrap(np.angle(hf))
    slope, icpt = np.polyfit(w, phase, 1)
    resid = phase - (slope * w + icpt)
    if np.sqrt(np.mean(resid**2)) > 0.2:
        return None
    m = float(np.sqrt(np.mean(mag**2)))
    w_all = 2 * np.pi * freqs / sample_rate
    return m * np.exp(1j * (slope * w_all + icpt))


def noise_bin_power(noise: np.ndarray | None, n_window: int, n_fft: int, sample_rate: int) -> np.ndarray | float:
    """Expected |N_k|² of the recording noise in an rfft of a window of ``n_window`` samples.

    Estimated from a noise-only region (the pre-roll) with Welch's method.
    """
    if noise is None or noise.size < 256 or not np.any(noise):
        return 0.0
    from scipy.signal import welch

    nper = min(4096, noise.size)
    f, S = welch(noise, sample_rate, nperseg=nper)
    grid = sfft.rfftfreq(n_fft, 1.0 / sample_rate)
    return n_window * sample_rate / 2 * np.interp(grid, f, S)


def deconvolve_sweep(y: np.ndarray, x: np.ndarray, sample_rate: int, f_lo: float, f_hi: float,
                     noise: np.ndarray | None = None, quant_power: np.ndarray | float = 0.0) -> SweepDeconvolution:
    """Wiener-regularized spectral division.

    ``y``: (frames, channels) rendered segment, ``x``: (frames,) dry segment with the same
    time origin. ``noise``: (frames, channels) noise-only part of the recording (pre-roll),
    ``quant_power``: per-sample power of the file's quantization noise (per channel).

    ``H = Y·X* / (|X|² + |N|²)``: exact wherever the sweep is well above the noise, gently
    suppressed where it isn't (below the start frequency or in noisy renders). Above the
    trusted band (see :func:`trusted_top`) H is either continued smoothly (flat, delay-like
    plugins) or tapered to zero at Nyquist.
    """
    y = y[:, None] if y.ndim == 1 else y
    n = max(y.shape[0], x.shape[0])
    n_fft = sfft.next_fast_len(2 * n, real=True)
    X = sfft.rfft(x, n_fft)
    P = (X * X.conj()).real
    f = sfft.rfftfreq(n_fft, 1.0 / sample_rate)
    nyq = sample_rate / 2
    band = (f >= f_lo) & (f <= f_hi)
    p_max = float(P[band].max())
    f_top = trusted_top(f_hi)
    eps_base = EPS_FLOOR * p_max + HF_EPS * p_max * _raised(f, f_hi, min(f_hi * 2 ** (1 / 24), nyq))
    taper = hf_taper(f, f_top, nyq)
    blend = _raised(f, f_top * 2 ** (-1 / 12), f_top)
    qp = np.broadcast_to(np.asarray(quant_power, dtype=float), (y.shape[1],))
    full = np.empty((y.shape[1], n_fft))
    gains = np.empty(y.shape[1])
    for c in range(y.shape[1]):
        nb = noise_bin_power(None if noise is None else noise[:, c], y.shape[0], n_fft, sample_rate)
        G = X.conj() / (P + eps_base + nb + y.shape[0] * qp[c])
        H = sfft.rfft(y[:, c], n_fft) * G
        cont = hf_continuation(H, f, sample_rate, f_top)
        if cont is not None:
            H = H * (1 - blend) + cont * blend
            G = G * (1 - blend)
        else:
            H = H * taper
            G = G * taper
        full[c] = sfft.irfft(H, n_fft)
        g2 = np.abs(G) ** 2
        total = g2[0] + g2[-1] + 2 * g2[1:-1].sum() if n_fft % 2 == 0 else g2[0] + 2 * g2[1:].sum()
        # white noise of variance s² in the n-sample window gives IR noise variance s²·n/N·Σ|G|²/N
        gains[c] = np.sqrt(total / n_fft * y.shape[0] / n_fft)
    return SweepDeconvolution(full, n_fft, gains)


def deconvolve_dirac(y: np.ndarray, impulse_pos: int, amplitude: float, neg: int, pos: int) -> RawIR:
    """IR = rendered samples around the impulse position / impulse amplitude."""
    y = y[:, None] if y.ndim == 1 else y
    out = np.zeros((y.shape[1], neg + pos))
    a, b = impulse_pos - neg, impulse_pos + pos
    sa, sb = max(a, 0), min(b, y.shape[0])
    if sb > sa:
        out[:, sa - a:sb - a] = y[sa:sb].T / amplitude
    return RawIR(out, neg)


# --- non-linearity ----------------------------------------------------------------------


@dataclass
class DistortionEstimate:
    thd_percent: float
    harmonics_db: dict[int, float]  # energy of each harmonic relative to the linear IR (dB)
    freqs: np.ndarray  # fundamental frequencies of the THD curve
    thd_curve_percent: np.ndarray


def _band_energy(segment: np.ndarray, sample_rate: int, centers: np.ndarray) -> np.ndarray:
    """Mean |H|² in 1/3-octave bands around ``centers`` (summed over channels)."""
    n = segment.shape[1]
    m = sfft.next_fast_len(max(n, 8192), real=True)
    w = np.ones(n)
    taper = max(1, n // 20)
    w[:taper] = 0.5 - 0.5 * np.cos(np.pi * (np.arange(taper) + 0.5) / taper)
    w[-taper:] = w[:taper][::-1]
    spec = np.sum(np.abs(sfft.rfft(segment * w, m, axis=1)) ** 2, axis=0)
    f = sfft.rfftfreq(m, 1.0 / sample_rate)
    out = np.zeros(centers.size)
    for i, fc in enumerate(centers):
        sel = (f >= fc / 2 ** (1 / 6)) & (f < fc * 2 ** (1 / 6))
        out[i] = spec[sel].mean() if sel.any() else 0.0
    return out


def estimate_distortion(dec: SweepDeconvolution, sample_rate: int, f_lo: float, f_hi: float, L: float,
                        main_len: int, max_harmonic: int = 5) -> DistortionEstimate | None:
    """THD from the harmonic IRs at negative time (Farina).

    With spectral division, harmonic ``k`` appears around ``-L·ln k`` and its spectrum at
    output frequency ``k·f`` equals the relative amplitude of that harmonic for input ``f``.
    """
    kmax = min(max_harmonic, int(f_hi // (2 * f_lo)))
    if kmax < 2:
        return None
    lo = max(2 * f_lo, 40.0)
    hi = f_hi / kmax
    if hi <= lo:
        hi = f_hi / 2
        kmax = 2
        if hi <= lo:
            return None
    n_bands = max(1, int(np.floor(3 * np.log2(hi / lo))) + 1)
    centers = lo * 2 ** (np.arange(n_bands) / 3)

    def seg_bounds(k: int) -> tuple[int, int]:
        t_k = -L * np.log(k)
        before = 0.5 * L * np.log((k + 1) / k)
        after = 0.5 * L * np.log(k / (k - 1)) if k > 1 else 0.0
        return int(round((t_k - before) * sample_rate)), int(round((t_k + after) * sample_rate))

    neg_main = int(round(0.5 * L * np.log(2) * sample_rate))
    e1 = _band_energy(dec.circular(-neg_main, neg_main + main_len), sample_rate, centers)
    total_h = np.zeros(centers.size)
    harmonics_db: dict[int, float] = {}
    e1_sum = float(e1.sum())
    for k in range(2, kmax + 1):
        a, b = seg_bounds(k)
        ek = _band_energy(dec.circular(a, b - a), sample_rate, centers * k)
        ek[centers * k > f_hi] = 0.0
        total_h += ek
        harmonics_db[k] = float(10 * np.log10(ek.sum() / e1_sum)) if e1_sum > 0 and ek.sum() > 0 else -300.0
    with np.errstate(divide="ignore", invalid="ignore"):
        curve = np.where(e1 > 0, 100 * np.sqrt(total_h / e1), 0.0)
    thd = 100 * np.sqrt(total_h.sum() / e1_sum) if e1_sum > 0 else 0.0
    return DistortionEstimate(float(thd), harmonics_db, centers, curve)


def system_response(session) -> RawIR:
    """What the capture chain itself measures for a perfect wire (used in tests and docs)."""
    from .signals import capture_signal, quantize

    lay, s = session.layout, session.settings
    x = np.zeros(lay.capture_tail_end)
    x[lay.capture_start:lay.capture_end] = capture_signal(session)
    x = quantize(x, s.test_bit_depth)
    if s.method.value == "dirac":
        return deconvolve_dirac(x, lay.capture_start, float(x[lay.capture_start]), lay.pre_roll, lay.ir_length)
    dec = deconvolve_sweep(x[:, None], x, s.sample_rate, s.f_start_hz, s.effective_f_end_hz,
                           quant_power=quantization_power(x, s.test_bit_depth.value))
    return dec.raw(lay.pre_roll, lay.ir_length)


def quantization_power(y: np.ndarray, subtype: str) -> np.ndarray:
    """Per-sample quantization noise power of a file format, per channel of ``y``."""
    y = y[:, None] if y.ndim == 1 else y
    s = subtype.upper()
    if s.startswith("PCM_") or s == "PCM24":
        bits = 24 if s == "PCM24" else int(s[4:]) if s[4:].isdigit() else 24
        return np.full(y.shape[1], (2.0 ** -(bits - 1)) ** 2 / 12)
    mean_power = np.mean(y**2, axis=0)
    if s in ("FLOAT", "FLOAT32"):
        return mean_power * 2.0**-48 / 3
    return mean_power * 2.0**-106 / 3
