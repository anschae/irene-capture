"""Round trips: synthetic plugin → simulated render → analysis → compare with the true IR."""

import numpy as np
import pytest

from conftest import band_error_db, capture, pad, raw_options
from irene_capture import testing as T
from irene_capture.core import AnalysisOptions
from irene_capture.core.presets import BitDepth, Method, StereoMode

SR = 48000


@pytest.mark.parametrize("method", [Method.SWEEP, Method.DIRAC])
def test_cabinet_ir_recovered(method):
    h = T.cab_ir()
    s, res = capture("cabinet", T.LTIPlugin(h), settings={"method": method}, options=raw_options())
    assert res.ok and res.ir_start == 0
    rec = res.ir[0, 0]
    assert band_error_db(rec, pad(h, rec.size), SR, 20, s.settings.effective_f_end_hz) < -80
    assert res.rating == "excellent"
    if method == Method.DIRAC:  # Dirac is exact over the full band
        err = 20 * np.log10(np.linalg.norm(rec - pad(h, rec.size)) / np.linalg.norm(h))
        assert err < -120


@pytest.mark.parametrize("plugin,ir", [
    (T.LTIPlugin(T.delay_ir(37)), T.delay_ir(37)),
    (T.LTIPlugin(T.gain_ir(0.5)), T.gain_ir(0.5)),
])
def test_flat_plugins_exact_full_band(plugin, ir):
    s, res = capture("eq", plugin, options=raw_options())
    rec = res.ir[0, 0]
    err = 20 * np.log10(np.linalg.norm(rec - pad(ir, rec.size)) / np.linalg.norm(ir))
    assert err < -80, err
    assert res.null_test.residual_db < -100
    assert res.ir_start == 0


def test_pure_delay_trimmed_and_kept():
    _, kept = capture("eq", T.LTIPlugin(T.delay_ir(200)))
    assert kept.ir_start == 0 and np.argmax(np.abs(kept.ir[0, 0])) == 200
    _, trimmed = capture("cabinet", T.LTIPlugin(T.delay_ir(200)))
    margin = int(0.0005 * SR)
    # the onset detector smooths a little, so the onset is found a few samples early
    assert 200 - margin - 6 <= trimmed.ir_start <= 200 - margin
    assert margin <= np.argmax(np.abs(trimmed.ir[0, 0])) <= margin + 6
    assert any(c.id == "latency_trim" for c in trimmed.checks)
    assert trimmed.null_test.rating == "excellent"  # latency added back in the null test


def test_reverb_stereo():
    h = T.reverb_ir()
    s, res = capture("reverb", T.LTIPlugin(h), options=raw_options())
    assert res.ir.shape[:2] == (1, 2) and res.channel_names == ["L", "R"]
    for c in range(2):
        rec = res.ir[0, c]
        assert band_error_db(rec, pad(h[c], rec.size), SR, 20, s.settings.effective_f_end_hz) < -80
    assert res.rating == "excellent"
    assert res.rt60_s == pytest.approx(1.2, rel=0.05)


def test_reverb_defaults_keep_predelay_and_trim_tail():
    _, res = capture("reverb", T.LTIPlugin(T.reverb_ir(predelay_s=0.02)))
    assert res.ir_start == 0
    first = np.flatnonzero(np.max(np.abs(res.ir[0]), axis=0) > 1e-3)[0]
    assert abs(first - 0.02 * SR) < 0.002 * SR  # pre-delay kept
    assert 1.5 * SR < res.length_samples < 3.0 * SR  # tail trimmed at the -100 dB point (RT60 1.2 s)
    assert res.tail.fade_len > 0 and not res.tail.truncated
    assert res.rating == "excellent"


def test_true_stereo():
    rv = T.reverb_ir()
    ts = np.stack([rv, 0.5 * rv[::-1]])  # (in, out, L)
    s, res = capture("reverb", T.LTIPlugin(ts), settings={"stereo_mode": StereoMode.TRUE_STEREO},
                     options=raw_options())
    assert res.ir.shape[:2] == (2, 2)
    assert res.channel_names == ["L→L", "L→R", "R→L", "R→R"]
    for i in range(2):
        for o in range(2):
            rec = res.ir[i, o]
            assert band_error_db(rec, pad(ts[i, o], rec.size), SR, 20, s.settings.effective_f_end_hz) < -80
    assert res.rating == "excellent"


def test_linear_phase_pre_ringing_kept():
    h = T.linear_phase_ir()
    s, res = capture("eq", T.LTIPlugin(h, advance=h.size // 2))
    assert res.ir_start < -400  # pre-ringing at negative time is kept
    assert res.rating == "excellent"


@pytest.mark.parametrize("bits", [BitDepth.FLOAT32, BitDepth.PCM24])
def test_latency_offset_and_formats(bits):
    h = T.cab_ir()
    s, res = capture("cabinet", T.LTIPlugin(h), latency=100, bit_depth=bits, stereo_out=True,
                     leading_silence=0, trailing_silence=3 * SR)
    assert res.ok
    assert any(c.id == "channels" and "identical" in c.message for c in res.checks)
    assert any(c.id == "length" and "longer" in c.message for c in res.checks)
    onset = int(np.flatnonzero(np.abs(h) >= 0.01 * np.max(np.abs(h)))[0])
    margin = int(0.0005 * SR)
    assert abs(res.ir_start - (100 + onset - margin)) <= 8
    assert res.rating == "excellent"


def test_export_offset_realigned():
    _, res = capture("cabinet", T.LTIPlugin(T.cab_ir()), export_offset=2 * SR)
    c = next(c for c in res.checks if c.id == "alignment")
    assert c.severity == "info" and c.conversion and "realigned" in c.message
    assert res.realigned_samples >= 2 * SR
    assert res.rating == "excellent"


def test_file_starting_late_realigned():
    _, res = capture("cabinet", T.LTIPlugin(T.cab_ir()), cut_start=int(0.2 * SR))
    assert res.realigned_samples < 0 and res.rating == "excellent"


def test_nonlinear_flagged():
    _, res = capture("cabinet", T.tanh_plugin())
    c = next(c for c in res.checks if c.id == "nonlinear")
    assert c.severity == "warning" and "non-linear" in c.message
    assert res.distortion.thd_percent > 1
    assert res.rating in ("audible differences possible", "unusable")
    assert any("non-linear" in h for h in res.hints)


def test_linear_plugin_not_flagged():
    _, res = capture("cabinet", T.LTIPlugin(T.cab_ir()))
    assert next(c for c in res.checks if c.id == "nonlinear").severity == "ok"
    assert res.distortion.thd_percent < 0.01


def test_time_variant_flagged():
    _, res = capture("cabinet", T.modulated_delay())
    assert res.rating in ("audible differences possible", "unusable")
    assert res.distortion.thd_percent < 1
    assert any("time-variant" in h for h in res.hints)


def test_truncated_ir_warns():
    _, res = capture("reverb", T.LTIPlugin(T.reverb_ir()), settings={"ir_length_s": 1.0})
    c = next(c for c in res.checks if c.id == "truncated")
    assert c.severity == "warning" and c.message == "IR was truncated — increase IR length."
    assert res.rating != "excellent"


def test_noisy_render_tail_stops_at_noise():
    _, res = capture("cabinet", T.LTIPlugin(T.cab_ir()), noise_dbfs=-90)
    assert res.ok and res.tail.noise_floor_db > -150
    assert res.tail.threshold_db == pytest.approx(res.tail.noise_floor_db + 6, abs=0.01) or \
        res.tail.threshold_db == -100


@pytest.mark.parametrize("rate", [44100, 96000])
def test_other_sample_rates(rate):
    _, res = capture("cabinet", T.LTIPlugin(T.cab_ir(rate)), settings={"sample_rate": rate})
    assert res.rating == "excellent"


def test_normalization_and_null_gain_undone():
    from irene_capture.core.presets import Normalization

    _, e = capture("cabinet", T.LTIPlugin(T.cab_ir()))
    assert e.normalization == Normalization.ENERGY
    assert np.sum(e.ir**2) == pytest.approx(1.0, rel=1e-6)
    assert e.rating == "excellent"  # null test undoes the gain
    _, p = capture("cabinet", T.LTIPlugin(T.cab_ir()),
                   options=AnalysisOptions(normalization=Normalization.PEAK, peak_target_dbfs=-3))
    assert 20 * np.log10(np.max(np.abs(p.ir))) == pytest.approx(-3, abs=1e-6)
    assert p.normalization_gain_db != 0


def test_ir_window_extends_for_pre_ringing():
    h = T.linear_phase_ir()
    _, res = capture("eq", T.LTIPlugin(h, advance=h.size // 2), options=raw_options())
    # the window grows by the pre-ringing instead of shifting: the full IR length after zero stays
    assert res.length_samples >= int(0.25 * SR) - res.ir_start - 1


def test_nonlinear_ir_starts_at_zero():
    _, res = capture("cabinet", T.tanh_plugin())
    assert res.ir_start >= 0
    peak = np.argmax(np.max(np.abs(res.ir[0]), axis=0))
    assert peak < int(0.002 * SR)  # the main (linear) part is in the IR
