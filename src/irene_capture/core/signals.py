"""Test signal design: exponential sine sweep, Dirac impulse, pink noise, full test file.

All signals are deterministic. The pink noise uses its own conversion of the PCG64 raw bit
stream, because NumPy guarantees the bit stream but not its distribution functions across
versions.
"""

from __future__ import annotations

import numpy as np

from .presets import BitDepth, Method
from .session import Layout, Session


def db_to_lin(db: float) -> float:
    return float(10 ** (db / 20))


def sweep_rate(f_start: float, f_end: float, length_s: float) -> float:
    """Farina's L: the sweep is f(t) = f_start * exp(t / L)."""
    return length_s / np.log(f_end / f_start)


def raised_cosine_fades(n: int, fade_in: int, fade_out: int) -> np.ndarray:
    w = np.ones(n)
    if fade_in > 0:
        k = np.arange(fade_in)
        w[:fade_in] = 0.5 - 0.5 * np.cos(np.pi * (k + 0.5) / fade_in)
    if fade_out > 0:
        k = np.arange(fade_out)
        w[n - fade_out:] *= 0.5 + 0.5 * np.cos(np.pi * (k + 0.5) / fade_out)
    return w


def exp_sweep(sample_rate: int, n: int, f_start: float, f_end: float, amplitude: float,
              fade_in_s: float = 0.05, fade_out_s: float = 0.01) -> np.ndarray:
    """Exponential sine sweep (Farina) with raised-cosine fades. Peak = ``amplitude``."""
    t = np.arange(n) / sample_rate
    length_s = n / sample_rate
    L = sweep_rate(f_start, f_end, length_s)
    x = np.sin(2 * np.pi * f_start * L * (np.exp(t / L) - 1.0))
    x *= raised_cosine_fades(n, int(round(fade_in_s * sample_rate)), int(round(fade_out_s * sample_rate)))
    return amplitude * x


def _uniform(seed: int, n: int) -> np.ndarray:
    raw = np.random.PCG64(seed).random_raw(n)
    return (raw >> np.uint64(11)).astype(np.float64) * (1.0 / 2**53)


def band_weight(f: np.ndarray, f_lo: float, f_hi: float, octaves_lo: float = 1 / 3,
                octaves_hi: float = 1 / 6) -> np.ndarray:
    """1 inside [f_lo * 2^octaves_lo, f_hi / 2^octaves_hi], raised-cosine (in log f) to 0 at f_lo/f_hi."""
    w = np.zeros_like(f)
    with np.errstate(divide="ignore"):
        lf = np.log2(np.maximum(f, 1e-12))
    a, b = np.log2(f_lo), np.log2(f_lo) + octaves_lo
    c, d = np.log2(f_hi) - octaves_hi, np.log2(f_hi)
    inside = (lf >= b) & (lf <= c)
    w[inside] = 1.0
    rise = (lf > a) & (lf < b)
    w[rise] = 0.5 - 0.5 * np.cos(np.pi * (lf[rise] - a) / (b - a))
    fall = (lf > c) & (lf < d)
    w[fall] = 0.5 + 0.5 * np.cos(np.pi * (lf[fall] - c) / (d - c))
    return w


def pink_noise(n: int, sample_rate: int, f_lo: float, f_hi: float, seed: int) -> np.ndarray:
    """Band-limited pink noise (−3 dB/octave between f_lo and f_hi), unit RMS."""
    f = np.fft.rfftfreq(n, 1.0 / sample_rate)
    mag = np.zeros_like(f)
    nz = f > 0
    mag[nz] = band_weight(f[nz], f_lo, f_hi) / np.sqrt(f[nz])
    phase = 2 * np.pi * _uniform(seed, f.size)
    x = np.fft.irfft(mag * np.exp(1j * phase), n)
    return x / np.sqrt(np.mean(x**2))


def verify_top(f_end: float) -> float:
    """Upper edge of the verification noise: the band in which the IR is known exactly."""
    return f_end * 2 ** (-1 / 12)


def capture_signal(session: Session) -> np.ndarray:
    s, lay, d = session.settings, session.layout, session.design
    amp = db_to_lin(s.test_level_dbfs)
    if s.method == Method.SWEEP:
        return exp_sweep(s.sample_rate, lay.capture_length, s.f_start_hz, s.effective_f_end_hz, amp,
                         d.sweep_fade_in_s, d.sweep_fade_out_s)
    x = np.zeros(lay.capture_length)
    x[0] = amp
    return x


def verify_signal(session: Session) -> np.ndarray:
    s, lay, d = session.settings, session.layout, session.design
    n = lay.verify_end - lay.verify_start
    x = pink_noise(n, s.sample_rate, s.f_start_hz, verify_top(s.effective_f_end_hz), d.noise_seed)
    peak_limit = db_to_lin(s.test_level_dbfs)
    gain = min(db_to_lin(d.verify_rms_dbfs), peak_limit / np.max(np.abs(x)))
    fade = int(round(d.verify_fade_s * s.sample_rate))
    return gain * x * raised_cosine_fades(n, fade, fade)


def quantize(x: np.ndarray, bit_depth: BitDepth) -> np.ndarray:
    """Exactly what ends up in the file (see :func:`audio_io.write_audio`)."""
    if bit_depth == BitDepth.PCM24:
        return np.clip(np.round(x * 2**23), -(2**23), 2**23 - 1) / 2**23
    return x.astype(np.float32).astype(np.float64)


def dry_pass(session: Session) -> np.ndarray:
    """One pass of the sequence as a mono signal (before channel placement), quantized."""
    lay = session.layout
    x = np.zeros(lay.pass_length)
    x[lay.capture_start:lay.capture_end] = capture_signal(session)
    x[lay.verify_start:lay.verify_end] = verify_signal(session)
    return quantize(x, session.settings.test_bit_depth)


def place_passes(mono_pass: np.ndarray, layout: Layout) -> np.ndarray:
    """Build the full test file (frames, channels) from one mono pass."""
    out = np.zeros((layout.total_length, layout.channels))
    for p in layout.passes:
        seg = slice(p.offset, p.offset + layout.pass_length)
        if p.input_channel is None:
            out[seg, :] = mono_pass[:, None]
        else:
            out[seg, p.input_channel] = mono_pass
    return out


def render_test_signal(session: Session) -> np.ndarray:
    """The complete test file as float64 (frames, channels), already quantized to its bit depth."""
    return place_passes(dry_pass(session), session.layout)
