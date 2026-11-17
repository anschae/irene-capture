"""Every import check from the spec, with its severity."""

import numpy as np
import pytest

from conftest import capture
from irene_capture import testing as T
from irene_capture.core import AnalysisOptions, CaptureSettings, NeedsDecision, analyze, create_session
from irene_capture.core import checks as ck
from irene_capture.core.analysis import Cancelled
from irene_capture.core.audio_io import RenderedAudio
from irene_capture.core.presets import BitDepth, StereoMode
from irene_capture.core.signals import render_test_signal

SR = 48000
CAB = T.LTIPlugin(T.cab_ir())


def by_id(res, cid):
    return [c for c in res.checks if c.id == cid]


def test_sample_rate_mismatch_is_error():
    s = create_session(CaptureSettings.for_type("cabinet"))
    r = T.simulate_render(CAB, render_test_signal(s), SR)
    r.sample_rate = 44100
    res = analyze(s, r)
    c = by_id(res, "sample_rate")[0]
    assert c.severity == "error" and "Project Setup" in c.detail and not res.ok


@pytest.mark.parametrize("subtype,sev", [("PCM_16", "warning"), ("PCM_24", "info"), ("PCM_32", "info"),
                                         ("FLOAT", "ok")])
def test_bit_depth(subtype, sev):
    c = ck.check_bit_depth(subtype)
    assert c.severity == sev
    assert c.conversion == (sev != "ok")


def test_stereo_identical_converted_to_mono():
    _, res = capture("cabinet", CAB, stereo_out=True)
    c = by_id(res, "channels")[0]
    assert c.severity == "info" and c.conversion and "identical" in c.message
    assert res.ir.shape[:2] == (1, 1)


def _differing_stereo(x, sr):
    y = CAB(x, sr)
    return np.concatenate([y, 0.5 * T.LTIPlugin(T.delay_ir(30))(x, sr)], axis=1)


def test_stereo_differs_needs_choice():
    s = create_session(CaptureSettings.for_type("cabinet"))
    r = T.simulate_render(_differing_stereo, render_test_signal(s), SR)
    with pytest.raises(NeedsDecision) as e:
        analyze(s, r)
    d = e.value.decision
    assert d.key == "channel" and {v for v, _ in d.choices} == {"left", "right", "sum"}
    assert "comb filtering" in d.question
    for choice in ("left", "right", "sum"):
        res = analyze(s, r, AnalysisOptions(decisions={"channel": choice}))
        c = by_id(res, "channels")[0]
        assert c.severity == "warning" and c.conversion
    with pytest.raises(Cancelled):
        analyze(s, r, AnalysisOptions(decisions={"channel": "cancel"}))
    right = analyze(s, r, AnalysisOptions(decisions={"channel": "right"}, trim_latency=False))
    assert np.argmax(np.abs(right.ir[0, 0])) == 30


def test_mono_file_for_stereo_capture():
    s = create_session(CaptureSettings.for_type("reverb"))
    x = render_test_signal(s)
    r = T.simulate_render(lambda x, sr: CAB(x[:, :1], sr), x, SR)
    with pytest.raises(NeedsDecision) as e:
        analyze(s, r)
    assert e.value.decision.key == "accept_mono"
    res = analyze(s, r, AnalysisOptions(decisions={"accept_mono": "continue"}))
    assert by_id(res, "channels")[0].severity == "warning"
    assert "mono" in by_id(res, "channels")[0].message and res.ir.shape[:2] == (1, 1)


def test_mono_file_for_true_stereo_is_error():
    s = create_session(CaptureSettings.for_type("reverb", stereo_mode=StereoMode.TRUE_STEREO))
    r = T.simulate_render(lambda x, sr: CAB(x[:, :1], sr), render_test_signal(s), SR)
    res = analyze(s, r)
    assert by_id(res, "channels")[0].severity == "error" and not res.ok


def test_too_short():
    s = create_session(CaptureSettings.for_type("cabinet"))
    lay = s.layout
    x = render_test_signal(s)
    r = T.simulate_render(CAB, x, SR, cut_end=lay.total_length - lay.capture_tail_end + 10)
    res = analyze(s, r)
    assert by_id(res, "length")[0].severity == "error" and not res.ok
    r = T.simulate_render(CAB, x, SR, cut_end=lay.total_length - lay.verify_end)
    res = analyze(s, r)
    c = by_id(res, "length")[0]
    assert c.severity == "warning" and "null test is skipped" in c.message
    assert res.ok and res.null_test is None


def test_longer_is_fine():
    _, res = capture("cabinet", CAB, trailing_silence=SR // 2)
    assert by_id(res, "length")[0].severity == "ok"
    _, res = capture("cabinet", CAB, trailing_silence=2 * SR)
    assert by_id(res, "length")[0].severity == "info"


def test_silent():
    s = create_session(CaptureSettings.for_type("cabinet"))
    r = RenderedAudio(np.zeros((s.layout.total_length, 1)), SR, "FLOAT")
    res = analyze(s, r)
    c = by_id(res, "silent")[0]
    assert c.severity == "error" and "no output" in c.message.lower() and "wrong track" in c.detail


def test_unprocessed():
    _, res = capture("eq", T.LTIPlugin(T.gain_ir(1.0)))
    c = by_id(res, "unprocessed")[0]
    assert c.severity == "error" and "bypassed" in c.message


def test_clipping_int_and_float():
    loud = T.LTIPlugin(T.gain_ir(4.0))
    _, res = capture("eq", loud, bit_depth=BitDepth.PCM24)
    assert by_id(res, "clipping")[0].severity == "error"
    _, res = capture("eq", loud)
    c = by_id(res, "clipping")[0]
    assert c.severity == "warning" and "float" in c.detail
    assert res.ok


def test_full_scale_runs():
    x = np.zeros((20, 1))
    x[5:7] = 1.0
    assert ck.full_scale_runs(x, 0.999) == 0
    x[10:13] = -1.0
    assert ck.full_scale_runs(x, 0.999) == 1


def test_missing_beginning():
    _, res = capture("cabinet", CAB, cut_start=int(0.6 * SR))
    c = by_id(res, "alignment")[0]
    assert c.severity == "error" and "missing" in c.message


def test_small_latency_is_kept_and_reported():
    _, res = capture("reverb", T.LTIPlugin(T.reverb_ir(predelay_s=0.05)))
    c = by_id(res, "latency")[0]
    assert c.severity == "info" and "pre-delay" in c.message
    assert res.realigned_samples == 0


def test_dirac_cannot_measure_distortion():
    from irene_capture.core.presets import Method

    _, res = capture("cabinet", CAB, settings={"method": Method.DIRAC})
    assert by_id(res, "nonlinear")[0].severity == "info"


def test_24bit_ir_that_would_clip_becomes_float():
    _, res = capture("eq", T.LTIPlugin(T.gain_ir(2.0)))
    c = by_id(res, "ir_format")[0]
    assert c.severity == "warning" and c.conversion
    assert res.ir_bit_depth == BitDepth.FLOAT32


def test_irene_length_note():
    _, res = capture("custom", T.LTIPlugin(T.gain_ir(0.5)), settings={"ir_length_s": 10.5, "tail_trim": False,
                                                                      "sweep_length_s": 2.0})
    assert any(c.id == "irene" for c in res.checks)


def test_messages_are_plain_sentences():
    _, res = capture("cabinet", T.tanh_plugin(), stereo_out=True)
    for c in res.checks:
        assert c.message[0].isupper() and c.message.rstrip().endswith((".", ")", "—")), c.message
