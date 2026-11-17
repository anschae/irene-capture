"""The analysis pipeline shared by all capture routes.

``analyze(session, rendered, options)`` runs every check, aligns the file, deconvolves,
post-processes and runs the null test. It never writes files; see :mod:`report`.

If a check needs the user's choice (e.g. which channel to use) and ``options`` doesn't
contain the answer, :class:`NeedsDecision` is raised. Answer it in
``options.decisions[decision.key]`` and call ``analyze`` again.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
from scipy.signal import correlate

from . import checks as ck
from .audio_io import RenderedAudio
from .checks import CheckResult, Decision, Severity, db, rms
from .deconvolution import RawIR, deconvolve_dirac, deconvolve_sweep, estimate_distortion, quantization_power
from .metrics import rt60
from .nulltest import compare, predict
from .postprocess import (
    NEGATIVE_CONTENT_DB,
    NEGATIVE_CONTENT_GAP_S,
    NEGATIVE_CONTENT_GUARD,
    PRE_ONSET_MARGIN_S,
    analyze_tail,
    find_early_onset,
    find_onset,
    half_hann_fade,
    normalization_gain,
)
from .presets import IRENE_MAX_LENGTH_S, BitDepth, Method, Normalization, StereoMode, TrueStereoOutput
from .result import CaptureResult
from .session import Session
from .signals import dry_pass, place_passes, sweep_rate, verify_top

THD_INFO_PERCENT = 0.1
THD_WARNING_PERCENT = 1.0
UNPROCESSED_DB = -60.0
NOISE_GUARD_S = 0.1  # the last part of the pre-roll is not used for the noise estimate
LATE_START_S = 0.001  # files whose content comes earlier than this are realigned


class NeedsDecision(Exception):
    def __init__(self, decision: Decision, checks: list[CheckResult]):
        super().__init__(decision.question)
        self.decision = decision
        self.checks = checks


class Cancelled(Exception):
    pass


@dataclass
class AnalysisOptions:
    decisions: dict[str, str] = field(default_factory=dict)
    trim_latency: bool | None = None  # None = from the session settings
    tail_trim: bool | None = None
    normalization: Normalization | None = None
    peak_target_dbfs: float | None = None
    ir_bit_depth: BitDepth | None = None
    extra_checks: list[CheckResult] = field(default_factory=list)  # from the backend (e.g. determinism)
    source_kind: str = "file"  # file | plugin


def _ms(samples: float, sr: int) -> str:
    return f"{samples / sr * 1000:.1f} ms"


def coarse_offset(rendered: np.ndarray, dry: np.ndarray) -> int:
    """Lag (samples) of the rendered file relative to the test file, by FFT cross-correlation."""
    c = correlate(rendered.astype(np.float32), dry.astype(np.float32), mode="full", method="fft")
    return int(np.argmax(np.abs(c))) - (dry.size - 1)


def _channel_names(n_in: int, n_out: int) -> list[str]:
    if n_in == 2:
        return ["L→L", "L→R", "R→L", "R→R"] if n_out == 2 else ["L→out", "R→out"]
    return ["mono"] if n_out == 1 else ["L", "R"]


def analyze(session: Session, rendered: RenderedAudio, options: AnalysisOptions | None = None) -> CaptureResult:
    opts = options or AnalysisOptions()
    s, lay = session.settings, session.layout
    sr = s.sample_rate
    result = CaptureResult(session, rendered.name or "rendered", rendered.path)
    out = result.checks
    result.trim_latency = s.trim_latency if opts.trim_latency is None else opts.trim_latency
    result.tail_trim = s.tail_trim if opts.tail_trim is None else opts.tail_trim
    result.normalization = s.normalization if opts.normalization is None else opts.normalization
    peak_target = s.peak_target_dbfs if opts.peak_target_dbfs is None else opts.peak_target_dbfs
    result.ir_bit_depth = s.ir_bit_depth if opts.ir_bit_depth is None else opts.ir_bit_depth

    # --- format ---------------------------------------------------------------------------
    sr_check = ck.check_sample_rate(rendered.sample_rate, sr)
    out.append(sr_check)
    if opts.source_kind == "file":
        out.append(ck.check_bit_depth(rendered.subtype))
    out.extend(opts.extra_checks)
    if sr_check.severity == Severity.ERROR:
        return result

    data = rendered.data
    if data.shape[1] > 2:
        out.append(CheckResult("channels", Severity.INFO,
                               f"The file has {data.shape[1]} channels; only the first two are used.",
                               conversion=True))
        data = data[:, :2]

    silent = ck.check_silent(data)
    out.append(silent)
    if silent.severity == Severity.ERROR:
        return result
    if opts.source_kind == "file":
        out.append(ck.check_clipping(data, rendered.is_float, rendered.bits))
    elif np.max(np.abs(data)) > 1.0:
        out.append(ck.check_clipping(data, True, None))

    # --- channels ---------------------------------------------------------------------------
    expected = session.output_channels_expected
    n_in = session.n_inputs
    if expected == 1 and data.shape[1] == 2:
        if ck.channels_identical(data):
            out.append(CheckResult("channels", Severity.INFO, "Converted to mono (channels were identical).",
                                   "Logic always bounces stereo; a mono track gives identical channels.",
                                   conversion=True))
            data = data.mean(axis=1, keepdims=True)
        else:
            decision = Decision(
                "channel",
                "This is a mono capture, but the left and right channels of the rendered file differ. "
                "Which channel should be used? Summing can cause comb filtering if the channels are "
                "delayed against each other (e.g. stereo widening or doubling in the plugin).",
                (("left", "Left"), ("right", "Right"), ("sum", "Sum (L+R)/2")),
                "left")
            choice = opts.decisions.get("channel")
            if choice is None:
                raise NeedsDecision(decision, out)
            if choice == "cancel":
                raise Cancelled()
            label = {"left": "left channel", "right": "right channel", "sum": "sum of both channels"}[choice]
            out.append(CheckResult(
                "channels", Severity.WARNING, f"Channels differ: using the {label}.",
                "For a mono capture, turn off stereo widening/doubling in the plugin, or capture in stereo.",
                conversion=True, decision=decision))
            data = {"left": data[:, :1], "right": data[:, 1:2],
                    "sum": data.mean(axis=1, keepdims=True)}[choice]
    elif expected == 2 and data.shape[1] == 1:
        if s.stereo_mode == StereoMode.TRUE_STEREO:
            out.append(CheckResult(
                "channels", Severity.ERROR, "True stereo needs a stereo render, but the file is mono.",
                "Put the test file on a stereo track, use a stereo plugin and export in stereo "
                "(Cubase: Mono Downmix off)."))
            return result
        decision = Decision("accept_mono", "The plugin output is mono, so the IR will be mono. Continue?",
                            (("continue", "Continue with a mono IR"),), "continue")
        choice = opts.decisions.get("accept_mono")
        if choice is None:
            raise NeedsDecision(decision, out)
        if choice == "cancel":
            raise Cancelled()
        out.append(CheckResult("channels", Severity.WARNING, "Plugin output is mono — IR will be mono.",
                               "If the plugin should be stereo, use a stereo track and turn off Mono Downmix.",
                               decision=decision))
    elif expected == 2 and ck.channels_identical(data):
        out.append(CheckResult("channels", Severity.INFO,
                               "Both output channels are identical: the plugin output is effectively mono.",
                               "The IR is written in stereo anyway. Check the plugin's stereo/width settings."))
    else:
        out.append(CheckResult("channels", Severity.OK, f"Channels OK ({data.shape[1]}).", ""))
    n_out = data.shape[1]

    # --- alignment ----------------------------------------------------------------------------
    dry = render_dry(session)
    pre = lay.pre_roll
    L = sweep_rate(s.f_start_hz, s.effective_f_end_hz, lay.capture_length / sr) if s.method == Method.SWEEP else 0.0
    neg_avail = min(pre, int(0.5 * L * np.log(2) * sr)) if s.method == Method.SWEEP else pre
    lag = coarse_offset(data.sum(axis=1), dry.sum(axis=1))
    result.offset_samples = lag
    if lag > pre:
        data = data[lag:]
        result.realigned_samples = lag
        out.append(CheckResult(
            "alignment", Severity.INFO,
            f"The rendered file starts {lag / sr:.3f} s before the test file; it was realigned automatically.",
            "The export range probably started before the test file. Any plugin latency or pre-delay can't be "
            "separated from this offset, so the IR starts at its first sound.", conversion=True))
    elif lag < -pre:
        out.append(CheckResult(
            "alignment", Severity.ERROR,
            f"The beginning of the rendered file is missing ({-lag / sr:.3f} s).",
            "The export range must start exactly at the start of the test file. In Cubase select the event and "
            "press P; in Logic select the region and press Cmd+U, then export again."))
        return result
    elif lag < -int(LATE_START_S * sr):
        data = np.concatenate([np.zeros((-lag, n_out)), data])
        result.realigned_samples = lag
        out.append(CheckResult(
            "alignment", Severity.INFO,
            f"The rendered file starts {_ms(-lag, sr)} late; it was realigned automatically.",
            "Make the export range start exactly at the start of the test file.", conversion=True))
    else:
        out.append(CheckResult("alignment", Severity.OK, "File is aligned with the test file.",
                               data={"offset_samples": lag}))

    # --- length -------------------------------------------------------------------------------
    last = lay.passes[-1]
    have = data.shape[0]
    need_capture = last.offset + lay.capture_tail_end
    need_verify = last.offset + lay.verify_tail_end
    verify_ok = True
    if have < need_capture:
        out.append(CheckResult(
            "length", Severity.ERROR,
            f"The rendered file is too short ({have / sr:.2f} s, the capture part needs {need_capture / sr:.2f} s).",
            "Export the whole test file: set the export range to the whole event/region "
            f"({lay.total_length / sr:.2f} s)."))
        return result
    if have < need_verify:
        verify_ok = False
        out.append(CheckResult(
            "length", Severity.WARNING,
            "The verification part at the end of the file is incomplete, so the null test is skipped.",
            f"Export the whole test file ({lay.total_length / sr:.2f} s) to get a quality check."))
    elif have - lay.total_length > sr:
        out.append(CheckResult("length", Severity.INFO,
                               f"The file is {(have - lay.total_length) / sr:.1f} s longer than the test file "
                               "(ignored)."))
    else:
        out.append(CheckResult("length", Severity.OK, "Length OK."))
    if have < lay.total_length:
        data = np.concatenate([data, np.zeros((lay.total_length - have, n_out))])
    data = data[:lay.total_length]

    # --- unprocessed ----------------------------------------------------------------------------
    ref = dry[:, :n_out] if dry.shape[1] >= n_out else np.repeat(dry[:, :1], n_out, axis=1)
    if n_out == 1 and dry.shape[1] == 2 and n_in == 1:
        ref = dry[:, :1]
    residual = db(rms(data - ref) / max(rms(ref), 1e-300))
    if residual < UNPROCESSED_DB:
        out.append(CheckResult(
            "unprocessed", Severity.ERROR, "The file seems unprocessed — plugin bypassed?",
            "The rendered file is practically identical to the test file. Check that the plugin is active "
            "(not bypassed) and that you exported the processed track, then render again."))
        return result

    # --- deconvolution ----------------------------------------------------------------------------
    pass_mono = dry_pass(session)
    pos = lay.ir_length
    raws: list[RawIR] = []
    noise_powers: list[float] = []
    distortion = None
    for p in lay.passes:
        y = data[p.offset:p.offset + lay.capture_tail_end]
        noise_region = y[:max(1, pre - int(NOISE_GUARD_S * sr))]
        sigma = np.sqrt(np.mean(noise_region**2, axis=0))
        if s.method == Method.SWEEP:
            x = pass_mono[:lay.capture_tail_end]
            dec = deconvolve_sweep(y, x, sr, s.f_start_hz, s.effective_f_end_hz, noise=noise_region,
                                   quant_power=quantization_power(y, rendered.subtype if opts.source_kind == "file"
                                                                  else "FLOAT"))
            raws.append(dec.raw(neg_avail, pos))
            noise_powers.extend((sigma * dec.noise_gain) ** 2)
            d = estimate_distortion(dec, sr, s.f_start_hz, s.effective_f_end_hz, L,
                                    int(round(s.ir_length_s * sr)))
            if d is not None and (distortion is None or d.thd_percent > distortion.thd_percent):
                distortion = d
        else:
            amp = float(pass_mono[lay.capture_start])
            raws.append(deconvolve_dirac(y, lay.capture_start, amp, neg_avail, pos))
            noise_powers.extend((sigma / abs(amp)) ** 2)
        result.noise_floor_dbfs = db(float(np.max(sigma)))
    raw = RawIR(np.concatenate([r.data for r in raws], axis=0), raws[0].zero)
    noise_power = float(np.mean(noise_powers))
    result.distortion = distortion
    if s.method == Method.SWEEP and distortion is not None:
        thd = distortion.thd_percent
        if thd > THD_WARNING_PERCENT:
            out.append(CheckResult(
                "nonlinear", Severity.WARNING,
                f"The plugin appears non-linear (≈ {thd:.1f} % THD) — the IR captures only the linear part.",
                "Amps, drive, saturation and compressors can't be captured as an IR. Turn off drive/saturation "
                "in the plugin if possible, or lower the test level.", data={"thd_percent": thd}))
        elif thd > THD_INFO_PERCENT:
            out.append(CheckResult("nonlinear", Severity.INFO, f"Slight non-linearity (≈ {thd:.2f} % THD).",
                                   "Usually inaudible in the IR.", data={"thd_percent": thd}))
        else:
            out.append(CheckResult("nonlinear", Severity.OK, f"Linear (THD ≈ {thd:.3f} %).",
                                   data={"thd_percent": thd}))
    elif s.method == Method.DIRAC:
        out.append(CheckResult("nonlinear", Severity.INFO,
                               "Non-linearity can't be measured with the Dirac method; the null test still checks it."))

    # --- post-processing ---------------------------------------------------------------------------
    margin = int(round(PRE_ONSET_MARGIN_S * sr))
    onset = find_onset(raw.data) - raw.zero
    # Content before zero delay (pre-ringing, a file that starts late) is looked for down to
    # −80 dB re peak or just above the noise, so that pre-ringing is kept completely.
    peak = float(np.max(np.abs(raw.data)))
    noise_rel = 20 * np.log10(4 * np.sqrt(noise_power) / peak) if noise_power > 0 and peak > 0 else -300.0
    early = find_early_onset(raw.data, onset + raw.zero, max(NEGATIVE_CONTENT_DB, noise_rel),
                             int(NEGATIVE_CONTENT_GAP_S * sr)) - raw.zero
    negative_content = early < -NEGATIVE_CONTENT_GUARD
    if negative_content:
        start = early - margin
    elif result.trim_latency:
        start = max(0, onset - margin)
    else:
        start = 0
    start = max(start, -raw.neg)
    if onset > 0.001 * sr and not result.realigned_samples:
        out.append(CheckResult(
            "latency", Severity.INFO,
            f"The output starts {_ms(onset, sr)} after the input: plugin latency or pre-delay.",
            "Trim latency removes it." if not result.trim_latency else ""))
    max_len = min(int(round(s.ir_length_s * sr)), raw.pos - start)
    h = raw.window(start, max_len)
    result.ir_start = start
    if result.trim_latency and start > 0:
        out.append(CheckResult("latency_trim", Severity.INFO, f"Latency trimmed: {_ms(start, sr)} ({start} samples).",
                               f"The IR starts {PRE_ONSET_MARGIN_S * 1000:g} ms before the first sound.",
                               conversion=True))
    elif start < 0 and not result.realigned_samples:
        out.append(CheckResult("latency_trim", Severity.INFO,
                               f"The IR includes {_ms(-start, sr)} before the zero-delay point.",
                               "This keeps pre-ringing (linear-phase filters). The IR is delayed by that amount "
                               "compared with the plugin."))

    tail = analyze_tail(h, sr, noise_power, result.tail_trim)
    result.tail = tail
    if result.tail_trim:
        h = half_hann_fade(h[:, :tail.length], tail.fade_len)
        if tail.length < max_len:
            out.append(CheckResult(
                "tail_trim", Severity.INFO,
                f"Tail trimmed to {tail.length / sr:.3f} s (decay reached {tail.threshold_db:.0f} dB), with a "
                f"{_ms(tail.fade_len, sr)} fade-out.", conversion=True))
        else:
            out.append(CheckResult("tail_trim", Severity.INFO,
                                   f"Full length kept ({tail.length / sr:.3f} s) with a {_ms(tail.fade_len, sr)} "
                                   "fade-out.", conversion=True))
    strongly_nonlinear = distortion is not None and distortion.thd_percent > THD_WARNING_PERCENT
    if tail.truncated and strongly_nonlinear:
        out.append(CheckResult("truncated", Severity.INFO,
                               "Can't tell whether the IR decayed: the distortion products cover the tail."))
    elif tail.truncated:
        out.append(CheckResult(
            "truncated", Severity.WARNING, "IR was truncated — increase IR length.",
            f"At the end of the IR ({max_len / sr:.3f} s) the response is still at {tail.end_level_db:.0f} dB "
            "re its peak. Choose a longer IR length and create a new test file."))
    else:
        out.append(CheckResult("truncated", Severity.OK, "The IR decays within the chosen length."))

    gain = normalization_gain(h, result.normalization, peak_target)
    result.normalization_gain_db = db(gain, floor=0.0) if gain != 1.0 else 0.0
    final = h * gain
    if result.normalization != Normalization.OFF:
        out.append(CheckResult("normalization", Severity.INFO,
                               f"Normalized ({result.normalization.value}): {result.normalization_gain_db:+.2f} dB.",
                               conversion=True))
    peak = float(np.max(np.abs(final)))
    if result.ir_bit_depth == BitDepth.PCM24 and peak >= 1.0:
        result.ir_bit_depth = BitDepth.FLOAT32
        out.append(CheckResult(
            "ir_format", Severity.WARNING,
            f"The IR peaks at {db(peak):+.1f} dBFS, so it is written as 32-bit float instead of 24-bit.",
            "A 24-bit file would clip. Use peak normalization if you need a 24-bit file.", conversion=True))

    result.ir = final.reshape(n_in, n_out, -1)
    result.channel_names = _channel_names(n_in, n_out)

    # --- IRene compatibility ---------------------------------------------------------------------
    if result.length_samples / sr > IRENE_MAX_LENGTH_S:
        out.append(CheckResult("irene", Severity.INFO,
                               f"IRene loads at most {IRENE_MAX_LENGTH_S:g} s; this IR is "
                               f"{result.length_samples / sr:.1f} s and will be cut there."))
    if n_in == 2 and s.true_stereo_output == TrueStereoOutput.FOUR_CHANNEL:
        out.append(CheckResult("irene", Severity.INFO,
                               "IRene uses only the first two channels of a 4-channel file.",
                               "For IRene, choose 'two stereo files' as true-stereo output."))

    # --- null test ---------------------------------------------------------------------------------
    hs = h.reshape(n_in, n_out, -1)
    if verify_ok:
        g = max(0, -start)
        preds, rends, cap_preds, cap_rends = [], [], [], []
        for i, p in enumerate(lay.passes):
            a, b = p.offset + lay.verify_start - g, p.offset + lay.verify_tail_end
            seg_dry = pass_mono[lay.verify_start - g:lay.verify_tail_end]
            preds.append(predict(seg_dry, hs[i], start, b - a))
            rends.append(data[a:b])
            ca, cb = p.offset, p.offset + lay.capture_tail_end
            cap_preds.append(predict(pass_mono[:lay.capture_tail_end], hs[i], start, cb - ca))
            cap_rends.append(data[ca:cb])
        pred, rend = np.concatenate(preds, axis=1), np.concatenate(rends, axis=1)
        band = (s.f_start_hz, verify_top(s.effective_f_end_hz))
        result.null_test = compare(pred, rend, sr, result.channel_names, band=band)
        result.capture_null = compare(np.concatenate(cap_preds, axis=1), np.concatenate(cap_rends, axis=1), sr,
                                      result.channel_names, with_details=False, band=band)
        nt = result.null_test
        sev = {"excellent": Severity.OK, "very good": Severity.OK,
               "audible differences possible": Severity.WARNING}.get(nt.rating, Severity.WARNING)
        out.append(CheckResult("null_test", sev, f"Null test: {nt.residual_db:.1f} dB — {nt.rating}.",
                               "Residual of the verification noise predicted with the IR, re the rendered level."))
        result.hints = interpretation_hints(result)

    if s.capture_type.value == "reverb" or result.length_samples > 0.3 * sr:
        result.rt60_s = rt60(final.reshape(-1, final.shape[-1]), sr)
    return result


def interpretation_hints(result: CaptureResult) -> list[str]:
    nt = result.null_test
    if nt is None or nt.rating == "excellent":
        return []
    hints = []
    thd = result.distortion.thd_percent if result.distortion else None
    truncated = result.tail is not None and result.tail.truncated and not any(
        c.id == "truncated" and c.severity == Severity.INFO for c in result.checks)
    if thd is not None and thd > THD_WARNING_PERCENT:
        hints.append(f"High distortion (≈ {thd:.1f} % THD): the plugin is non-linear. An IR can't reproduce "
                     "drive, saturation or compression.")
    elif truncated:
        hints.append("The IR was truncated — increase the IR length. If the residual stays high after that, the "
                     "plugin is probably time-variant (modulation, chorus, randomization).")
    else:
        hints.append("Low distortion but a high residual: the plugin is probably time-variant "
                     "(modulation, chorus, randomization) — turn those off and capture again.")
        if result.realigned_samples:
            hints.append("The file was realigned; check that the export range starts exactly at the test file.")
    if any(c.id == "determinism" and c.severity != Severity.OK for c in result.checks):
        hints.append("The plugin gave different output on two identical runs (time-variant/random).")
    return hints


def render_dry(session: Session) -> np.ndarray:
    return place_passes(dry_pass(session), session.layout)
