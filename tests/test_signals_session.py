import json

import numpy as np
import pytest

from irene_capture.core import CaptureSettings, create_session, load_session, render_test_signal
from irene_capture.core.checks import db, rms
from irene_capture.core.presets import (
    PRESETS,
    BitDepth,
    CaptureType,
    Method,
    Normalization,
    StereoMode,
    validate,
)
from irene_capture.core.session import SCHEMA_VERSION, compute_layout
from irene_capture.core.signals import exp_sweep, pink_noise, verify_signal


def test_presets_defaults():
    assert CaptureSettings.for_type("cabinet").ir_length_s == 0.2
    r = CaptureSettings.for_type("reverb")
    assert r.stereo_mode == StereoMode.STEREO and r.ir_length_s == 4.0 and not r.trim_latency
    eq = CaptureSettings.for_type("eq")
    assert eq.normalization == Normalization.OFF and not eq.trim_latency
    assert CaptureSettings.for_type("custom", stereo_mode=StereoMode.STEREO).input_channels == 2
    assert PRESETS[CaptureType.CABINET].normalization == Normalization.ENERGY


def test_validation_messages():
    msgs = validate(CaptureSettings.for_type("reverb", ir_length_s=0.2))
    assert msgs[0].severity == "warning"
    assert msgs[0].text == ("0.2 s is very short for a reverb; typical reverbs need 1–10 s. "
                            "The tail will be cut off.")
    assert validate(CaptureSettings.for_type("cabinet")) == []
    assert any(m.severity == "error" for m in validate(CaptureSettings.for_type("cabinet", sweep_length_s=0.5)))
    assert any(m.severity == "error" for m in validate(CaptureSettings.for_type("cabinet", test_level_dbfs=3)))
    assert any(m.severity == "error" for m in validate(CaptureSettings.for_type("cabinet",
                                                                               stereo_mode=StereoMode.STEREO)))
    assert any(m.severity == "info" for m in validate(CaptureSettings.for_type("cabinet",
                                                                              test_bit_depth=BitDepth.PCM24)))
    assert any("IRene" in m.text for m in validate(CaptureSettings.for_type("reverb", ir_length_s=12)))
    with pytest.raises(ValueError):
        create_session(CaptureSettings.for_type("cabinet", sweep_length_s=40))


def test_sweep_level_and_fades():
    x = exp_sweep(48000, 48000, 20, 22800, 0.5)
    assert abs(np.max(np.abs(x)) - 0.5) < 1e-3
    assert abs(x[0]) < 1e-3 and abs(x[-1]) < 1e-3


def test_pink_noise_deterministic_and_pink():
    a = pink_noise(48000, 48000, 20, 20000, 7)
    b = pink_noise(48000, 48000, 20, 20000, 7)
    assert np.array_equal(a, b)
    assert abs(rms(a) - 1) < 1e-9
    f = np.fft.rfftfreq(a.size, 1 / 48000)
    p = np.abs(np.fft.rfft(a)) ** 2
    lo = p[(f > 200) & (f < 400)].sum()
    hi = p[(f > 2000) & (f < 4000)].sum()
    assert abs(10 * np.log10(hi / lo)) < 0.5  # equal energy per octave


def test_verify_signal_levels():
    s = create_session(CaptureSettings.for_type("cabinet"))
    v = verify_signal(s)
    assert abs(db(rms(v)) - (-20)) < 0.5
    assert np.max(np.abs(v)) <= 10 ** (-6 / 20) + 1e-12
    s2 = create_session(CaptureSettings.for_type("cabinet", test_level_dbfs=-24))
    assert np.max(np.abs(verify_signal(s2))) <= 10 ** (-24 / 20) + 1e-12


def test_layout_offsets():
    s = create_session(CaptureSettings.for_type("cabinet"))
    lay = s.layout
    sr = 48000
    assert lay.pre_roll == sr // 2 and lay.capture_start == lay.pre_roll
    assert lay.capture_length == 6 * sr
    assert lay.capture_tail_end - lay.capture_end == int(0.7 * sr)
    assert lay.verify_end - lay.verify_start == 4 * sr
    assert lay.pass_length == lay.verify_tail_end + sr // 2
    x = render_test_signal(s)
    assert x.shape == (lay.total_length, 1)
    assert np.all(x[: lay.pre_roll] == 0) and np.all(x[lay.capture_end:lay.verify_start] == 0)
    d = create_session(CaptureSettings.for_type("cabinet", method=Method.DIRAC))
    xd = render_test_signal(d)
    assert np.count_nonzero(xd[: d.layout.verify_start]) == 1


def test_stereo_and_true_stereo_test_files():
    s = create_session(CaptureSettings.for_type("reverb"))
    x = render_test_signal(s)
    assert x.shape[1] == 2 and np.array_equal(x[:, 0], x[:, 1])
    t = create_session(CaptureSettings.for_type("reverb", stereo_mode=StereoMode.TRUE_STEREO))
    x = render_test_signal(t)
    n = t.layout.pass_length
    assert x.shape == (2 * n, 2)
    assert np.all(x[:n, 1] == 0) and np.all(x[n:, 0] == 0)
    assert np.array_equal(x[:n, 0], x[n:, 1])


def test_pcm24_test_signal_is_quantized():
    s = create_session(CaptureSettings.for_type("cabinet", test_bit_depth=BitDepth.PCM24))
    x = render_test_signal(s)
    assert np.allclose(x * 2**23, np.round(x * 2**23))


def test_session_json_roundtrip(tmp_path):
    s = create_session(CaptureSettings.for_type("reverb", stereo_mode=StereoMode.TRUE_STEREO))
    p = s.save(tmp_path / "x.wav.capture.json")
    s2 = load_session(p)
    assert s2.to_dict() == s.to_dict()
    assert s2.layout == compute_layout(s2.settings)
    assert s.short_id in s.test_file and s.test_file.startswith("irc_reverb_sweep_48k")
    assert np.array_equal(render_test_signal(s), render_test_signal(s2))
    d = json.loads(p.read_text())
    assert d["schema_version"] == SCHEMA_VERSION and d["tool_version"] and d["layout"]["passes"]
    for key in ("repetitions", "loopback_channel", "input_device", "output_device"):
        assert key in d["hardware"]


def test_session_rejects_bad_files(tmp_path):
    s = create_session(CaptureSettings.for_type("cabinet"))
    d = s.to_dict()
    d["layout"]["capture_start"] += 1
    (tmp_path / "bad.json").write_text(json.dumps(d))
    with pytest.raises(ValueError, match="layout"):
        load_session(tmp_path / "bad.json")
    d = s.to_dict()
    d["schema_version"] = SCHEMA_VERSION + 1
    (tmp_path / "new.json").write_text(json.dumps(d))
    with pytest.raises(ValueError, match="newer"):
        load_session(tmp_path / "new.json")
    (tmp_path / "junk.json").write_text("{")
    with pytest.raises(ValueError):
        load_session(tmp_path / "junk.json")
