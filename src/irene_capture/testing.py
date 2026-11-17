"""Synthetic "plugins" with known behavior and a simulated DAW render.

Used by the tests and by ``irene-capture --selftest``. Each plugin maps a (frames, channels)
input to a (frames, channels_out) output at a given sample rate.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

import numpy as np
from scipy.signal import fftconvolve, firwin

from .core.audio_io import RenderedAudio
from .core.presets import BitDepth
from .core.signals import quantize

Plugin = Callable[[np.ndarray, int], np.ndarray]


@dataclass
class LTIPlugin:
    """Convolution with a known IR. ``ir`` is (outputs, L) or (inputs, outputs, L).

    ``advance`` shifts the output earlier (models a DAW's latency compensation of a
    linear-phase filter, so pre-ringing lands before time zero).
    """

    ir: np.ndarray
    advance: int = 0

    def __call__(self, x: np.ndarray, sr: int) -> np.ndarray:
        ir = self.ir.reshape((1,) * (3 - self.ir.ndim) + self.ir.shape)
        n_in, n_out, _ = ir.shape
        out = np.zeros((x.shape[0], n_out))
        for i in range(n_in):
            xi = x[:, min(i, x.shape[1] - 1)] if n_in > 1 else x.mean(axis=1) if x.shape[1] > 1 else x[:, 0]
            for o in range(n_out):
                y = fftconvolve(xi, ir[i, o])[self.advance:]
                n = min(y.size, out.shape[0])
                out[:n, o] += y[:n]
        return out


def cab_ir(sr: int = 48000, taps: int = 4096, seed: int = 1) -> np.ndarray:
    """Cabinet-like FIR: noise with a fast exponential decay, low-passed, 4096 taps."""
    rng = np.random.default_rng(seed)
    t = np.arange(taps) / sr
    h = rng.standard_normal(taps) * np.exp(-t / 0.004)
    h = fftconvolve(h, firwin(63, 6000, fs=sr))[:taps]
    h[:3] = 0  # tiny acoustic delay
    return h / np.sqrt(np.sum(h**2)) * 0.25  # about -12 dB on broadband material


def reverb_ir(sr: int = 48000, length_s: float = 3.0, rt60_s: float = 1.2, predelay_s: float = 0.02,
              seed: int = 2) -> np.ndarray:
    """Decaying stereo noise "reverb" (outputs=2). Decays ~ 150 dB over 3 s."""
    rng = np.random.default_rng(seed)
    n = int(length_s * sr)
    t = np.arange(n) / sr
    env = 10 ** (-3 * t / rt60_s)
    h = rng.standard_normal((2, n)) * env
    h *= 0.5 / np.sqrt(np.sum(h**2, axis=1, keepdims=True))
    pd = int(predelay_s * sr)
    return np.concatenate([np.zeros((2, pd)), h[:, : n - pd]], axis=1)


def linear_phase_ir(sr: int = 48000, taps: int = 1023) -> np.ndarray:
    """Symmetric (linear-phase) FIR: a gentle high shelf/bell mix with pre-ringing."""
    lp = firwin(taps, 2000, fs=sr)
    delta = np.zeros(taps)
    delta[taps // 2] = 1.0
    return delta + 0.8 * lp - 0.4 * firwin(taps, [300, 900], fs=sr, pass_zero=False)


def delay_ir(samples: int = 37) -> np.ndarray:
    h = np.zeros(samples + 1)
    h[samples] = 1.0
    return h


def gain_ir(gain: float = 0.5) -> np.ndarray:
    return np.array([gain])


def tanh_plugin(drive: float = 4.0) -> Plugin:
    def f(x: np.ndarray, sr: int) -> np.ndarray:
        return np.tanh(drive * x) / drive
    return f


def modulated_delay(depth_ms: float = 1.0, rate_hz: float = 0.7, base_ms: float = 3.0) -> Plugin:
    """Chorus-like time-variant delay (linear interpolation)."""
    def f(x: np.ndarray, sr: int) -> np.ndarray:
        n = x.shape[0]
        t = np.arange(n)
        d = (base_ms + depth_ms * np.sin(2 * np.pi * rate_hz * t / sr)) * sr / 1000
        pos = t - d
        i0 = np.floor(pos).astype(int)
        frac = pos - i0
        out = np.zeros_like(x)
        ok = (i0 >= 0) & (i0 + 1 < n)
        for c in range(x.shape[1]):
            out[ok, c] = (1 - frac[ok]) * x[i0[ok], c] + frac[ok] * x[i0[ok] + 1, c]
        return out
    return f


def simulate_render(plugin: Plugin, test_signal: np.ndarray, sr: int, *, latency: int = 0, export_offset: int = 0,
                    leading_silence: int = 0, trailing_silence: int = 0, bit_depth: BitDepth = BitDepth.FLOAT32,
                    stereo_out: bool = False, cut_start: int = 0, cut_end: int = 0, name: str = "rendered",
                    noise_dbfs: float | None = None, seed: int = 5) -> RenderedAudio:
    """Run ``plugin`` like a DAW would and return what the exported file would contain."""
    y = plugin(test_signal, sr)
    if stereo_out and y.shape[1] == 1:
        y = np.repeat(y, 2, axis=1)
    if latency:
        y = np.concatenate([np.zeros((latency, y.shape[1])), y])[: y.shape[0]]
    pad = export_offset + leading_silence
    if pad:
        y = np.concatenate([np.zeros((pad, y.shape[1])), y])
    if trailing_silence:
        y = np.concatenate([y, np.zeros((trailing_silence, y.shape[1]))])
    if noise_dbfs is not None:
        y = y + 10 ** (noise_dbfs / 20) * np.random.default_rng(seed).standard_normal(y.shape)
    if cut_start:
        y = y[cut_start:]
    if cut_end:
        y = y[:-cut_end]
    subtype = "PCM_24" if bit_depth == BitDepth.PCM24 else "FLOAT"
    if bit_depth == BitDepth.PCM24:
        y = np.clip(y, -1.0, 1.0 - 2**-23)
    return RenderedAudio(quantize(y, bit_depth), sr, subtype, None, name)
