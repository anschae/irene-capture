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


def test_recovers_after_worker_crash():
    h = PluginHostBackend()
    try:
        h.load("builtin:Gain:gain_db=-6")
        h._proc.kill()  # simulate a crashing plugin
        h._proc.join(5)
        # next call restarts the worker and reloads the plugin transparently
        res = h.capture_and_analyze(session("eq"))
        assert res.ok and res.rating == "excellent"
        assert res.plugin_info["name"] == "Gain" and h.crashes == 0
    finally:
        h.stop()


def test_resolve_windows_bundle(tmp_path, monkeypatch):
    import sys

    from irene_capture.backends import plugin_host

    b = tmp_path / "X.vst3" / "Contents" / "x86_64-win"
    b.mkdir(parents=True)
    (b / "X.vst3").write_bytes(b"")
    monkeypatch.setattr(sys, "platform", "win32")
    assert plugin_host.resolve_plugin_path(str(tmp_path / "X.vst3")) == str(b / "X.vst3")
    monkeypatch.setattr(sys, "platform", "darwin")
    assert plugin_host.resolve_plugin_path(str(tmp_path / "X.vst3")) == str(tmp_path / "X.vst3")


def test_candidates_bundle_first(tmp_path, monkeypatch):
    import sys

    from irene_capture.backends import plugin_host

    b = tmp_path / "Y.vst3" / "Contents" / "x86_64-win"
    b.mkdir(parents=True)
    (b / "Y.vst3").write_bytes(b"")
    monkeypatch.setattr(sys, "platform", "win32")
    assert plugin_host.plugin_path_candidates(str(tmp_path / "Y.vst3")) == [str(tmp_path / "Y.vst3"), str(b / "Y.vst3")]
    assert plugin_host._is_scan_failure(ImportError("Unable to scan plugin x: unsupported plugin format or scan failure."))


def test_worker_exits_when_app_goes_away():
    """If the app closes its end of the pipes (quit, crash), the worker must exit by itself,
    without trying to report an error (that showed an 'unhandled exception' dialog on Windows)."""
    import os
    import time

    h = PluginHostBackend()
    h.load("builtin:Gain:gain_db=-6")
    pid = h._worker_pid
    assert pid and pid != os.getpid()
    h._close_pipes()  # like the app disappearing
    for _ in range(50):
        if not h._proc.is_alive():
            break
        time.sleep(0.1)
    assert not h._proc.is_alive()
    assert "worker started" in h.log_path.read_text()


def test_timeout_kills_worker(monkeypatch):
    from irene_capture.backends import plugin_host

    h = PluginHostBackend()
    try:
        h.load("builtin:Gain:gain_db=-6")
        proc = h._proc
        with pytest.raises(PluginError, match="did not respond"):
            h._call({"cmd": "sleep", "seconds": 30}, timeout=1)
        proc.join(5)
        assert not proc.is_alive() and h._proc is None
        # next call starts a new worker and reloads the plugin
        assert h.parameters() is not None and h.info["name"] == "Gain"
    finally:
        h.stop()
    assert plugin_host.LOAD_TIMEOUT_S >= 60
