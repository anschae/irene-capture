"""Direct route through the real worker process, with pedalboard's built-in effects."""

import pytest

from irene_capture.backends.plugin_host import PluginError, PluginHostBackend, scan_plugins
from irene_capture.core import CaptureSettings, create_session, write_outputs


@pytest.fixture(scope="module")
def host():
    h = PluginHostBackend()
    h.start()
    yield h
    h.stop()


def session(ctype, **kw):
    kw.setdefault("sweep_length_s", 2.0)
    return create_session(CaptureSettings.for_type(ctype, **kw), source="plugin")


def test_gain(host):
    info = host.load("builtin:Gain:gain_db=-6")
    assert info["name"] == "Gain"
    res = host.capture_and_analyze(session("eq"))
    assert res.ok and res.rating == "excellent"
    assert abs(20 * __import__("numpy").log10(abs(res.ir[0, 0]).max()) + 6) < 0.01
    assert any(c.id == "determinism" and c.severity == "ok" for c in res.checks)
    assert not any(c.id == "bit_depth" for c in res.checks)


def test_filter(host):
    host.load("builtin:LowpassFilter:cutoff_frequency_hz=2000")
    res = host.capture_and_analyze(session("eq"))
    assert res.ok and res.rating == "excellent"


def test_reverb_stereo(host, tmp_path):
    host.load("builtin:Reverb:room_size=0.3,wet_level=1.0,dry_level=0.0,width=1.0")
    s = session("reverb", ir_length_s=3.0, capture_name="room")
    res = host.capture_and_analyze(s)
    assert res.ok and res.ir.shape[:2] == (1, 2)
    assert res.rating in ("excellent", "very good")
    files = write_outputs(res, tmp_path)
    assert files.ir_files[0].name == "Reverb_room_ir.wav"
    assert host.save_state(files.ir_files[0]) == []  # built-ins have no state to save


def test_unknown_builtin(host):
    with pytest.raises(PluginError):
        host.load("builtin:NoSuchThing")
    host.load("builtin:Gain:gain_db=-3")  # host still usable


def test_missing_file(host, tmp_path):
    with pytest.raises(PluginError):
        host.load(str(tmp_path / "missing.vst3"))


def test_scan_finds_bundles(tmp_path):
    (tmp_path / "Vendor" / "A.vst3" / "Contents").mkdir(parents=True)
    (tmp_path / "B.vst3").write_bytes(b"")
    found = [p.name for p in scan_plugins([tmp_path])]
    assert "A.vst3" in found and "B.vst3" in found
