import json

import numpy as np
import pytest
import soundfile as sf

from conftest import capture
from irene_capture import testing as T
from irene_capture.backends.daw import (
    DawRoundtripBackend,
    SessionIndex,
    find_session,
    instructions_text,
    render_instructions,
    short_ids_in,
)
from irene_capture.cli import main as cli
from irene_capture.core import CaptureSettings, create_session, load_session, write_outputs
from irene_capture.core.audio_io import read_audio, write_audio
from irene_capture.core.presets import BitDepth, Normalization, StereoMode, TrueStereoOutput

SR = 48000
CAB = T.LTIPlugin(T.cab_ir())


def test_write_outputs_never_overwrites(tmp_path):
    _, res = capture("cabinet", CAB, name="Mixdown")
    a = write_outputs(res, tmp_path)
    b = write_outputs(res, tmp_path)
    assert a.ir_files[0].name == "Mixdown_ir.wav" and b.ir_files[0].name == "Mixdown_ir_2.wav"
    assert a.json_file.name == "Mixdown_ir.json" and a.txt_file.name == "Mixdown_ir.txt"
    rep = json.loads(a.json_file.read_text())
    for key in ("session_id", "settings", "checks", "conversions", "offset_samples", "trimmed_latency_samples",
                "normalization_gain_db", "tail", "distortion", "null_test", "rating", "tool_version"):
        assert key in rep
    assert rep["rating"] == "excellent"
    info = sf.info(str(a.ir_files[0]))
    assert info.subtype == "PCM_24" and info.channels == 1 and info.samplerate == SR
    with sf.SoundFile(str(a.ir_files[0])) as f:
        assert "irene-capture" in (f.comment or "") or "irene-capture" in (f.software or "")
    ir = read_audio(a.ir_files[0]).data[:, 0]
    assert np.max(np.abs(ir - res.ir[0, 0])) < 2**-22
    assert "Null test" in a.txt_file.read_text()


def test_true_stereo_outputs(tmp_path):
    rv = T.reverb_ir()
    ts = np.stack([rv, 0.5 * rv])
    _, res = capture("reverb", T.LTIPlugin(ts), settings={"stereo_mode": StereoMode.TRUE_STEREO}, name="hall")
    two = write_outputs(res, tmp_path)
    assert [p.name for p in two.ir_files] == ["hall_ir_inL.wav", "hall_ir_inR.wav"]
    assert all(sf.info(str(p)).channels == 2 for p in two.ir_files)
    _, res4 = capture("reverb", T.LTIPlugin(ts), settings={"stereo_mode": StereoMode.TRUE_STEREO,
                                                           "true_stereo_output": TrueStereoOutput.FOUR_CHANNEL},
                      name="hall4")
    four = write_outputs(res4, tmp_path)
    assert sf.info(str(four.ir_files[0])).channels == 4


def test_write_refuses_errors(tmp_path):
    _, res = capture("eq", T.LTIPlugin(T.gain_ir(1.0)))
    with pytest.raises(ValueError):
        write_outputs(res, tmp_path)


def test_instructions_filled_in():
    s = create_session(CaptureSettings.for_type("reverb"))
    ins = render_instructions(s)
    assert set(ins) == {"Cubase", "Logic Pro"}
    assert "48 kHz" in ins["Cubase"] and "Mono Downmix: off" in ins["Cubase"]
    assert "100 % wet" in ins["Cubase"] and "Cab sims" not in ins["Cubase"]
    assert "{" not in ins["Logic Pro"] and "?" not in "".join(line[:1] for line in ins["Logic Pro"].splitlines())
    cab = render_instructions(create_session(CaptureSettings.for_type("cabinet", sample_rate=96000)))
    assert "96 kHz" in cab["Logic Pro"] and "Mono Downmix: on" in cab["Cubase"] and "Cab sims" in cab["Cubase"]
    eq = render_instructions(create_session(CaptureSettings.for_type("eq")))
    assert "pan law" in eq["Cubase"].lower()
    assert "**" not in instructions_text(s)


def test_session_lookup(tmp_path):
    index = SessionIndex(tmp_path / "index.json")
    daw = DawRoundtripBackend(index)
    s1 = create_session(CaptureSettings.for_type("cabinet"))
    s2 = create_session(CaptureSettings.for_type("reverb"))
    while s2.short_id == s1.short_id:
        s2 = create_session(CaptureSettings.for_type("reverb"))
    daw.create_test_file(s1, tmp_path / "tests" / s1.test_file)
    daw.create_test_file(s2, tmp_path / "tests" / s2.test_file)
    renders = tmp_path / "renders"
    renders.mkdir()
    found, how = find_session(renders / f"{s1.test_file[:-4]}-01.wav", index)
    assert found.session_id == s1.session_id and "ID" in how
    found, how = find_session(renders / "Audio 01.wav", index)
    assert found.session_id == s2.session_id and "most recent" in how
    found, how = find_session(renders / "x.wav", SessionIndex(tmp_path / "empty.json"))
    assert found is None and "Choose" in how
    assert short_ids_in("irc_cab_48k_7f3a_bounce") == ["7f3a"]


def test_test_file_on_disk(tmp_path):
    s = create_session(CaptureSettings.for_type("reverb", test_bit_depth=BitDepth.PCM24))
    wav, js, txt = DawRoundtripBackend().create_test_file(s, tmp_path / s.test_file)
    info = sf.info(str(wav))
    assert info.channels == 2 and info.subtype == "PCM_24" and info.frames == s.layout.total_length
    assert js.name == wav.name + ".capture.json" and load_session(js).session_id == s.session_id
    assert txt.name.endswith("_instructions.txt") and "Cubase" in txt.read_text()


def test_cli_generate_and_analyze(tmp_path, capsys):
    assert cli(["generate", "--type", "eq", "--sweep-length", "2", "--out", str(tmp_path)]) == 0
    wav = next(tmp_path.glob("irc_eq_*.wav"))
    test = read_audio(wav)
    r = T.simulate_render(T.LTIPlugin(T.cab_ir()), test.data, SR, stereo_out=True)
    rendered = tmp_path / (wav.stem + "_bounce.wav")
    write_audio(rendered, r.data, SR, BitDepth.FLOAT32)
    assert cli(["analyze", str(rendered)]) == 0
    out = capsys.readouterr().out
    assert "excellent" in out and "found by the ID" in out
    assert (tmp_path / (rendered.stem + "_ir.wav")).exists()
    assert cli(["analyze", str(rendered), "--dry-run", "--session", str(wav) + ".capture.json"]) == 0


def test_cli_decision_flag(tmp_path, capsys):
    s = create_session(CaptureSettings.for_type("cabinet", sweep_length_s=2.0,
                                                normalization=Normalization.OFF))
    wav, js, _ = DawRoundtripBackend().create_test_file(s, tmp_path / s.test_file)
    x = read_audio(wav).data
    y = np.concatenate([CAB(x, SR), T.LTIPlugin(T.delay_ir(5))(x, SR)], axis=1)
    rendered = tmp_path / "differs.wav"
    write_audio(rendered, y, SR, BitDepth.FLOAT32)
    assert cli(["analyze", str(rendered), "--session", str(js)]) == 3
    assert cli(["analyze", str(rendered), "--session", str(js), "--channel", "left"]) == 0


def test_cli_selftest_parts():
    from irene_capture.selftest import daw_route

    assert "null test" in daw_route()
