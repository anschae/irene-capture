"""``irene-capture --selftest``: headless end-to-end check of a build.

1. DAW route: create a test file on disk, read it back, render it through a synthetic
   cabinet, analyze, write the IR, expect "excellent".
2. Direct route: pedalboard's built-in Gain through the worker process (checks that
   ``spawn`` works in the frozen app).
3. GUI: build the main window and close it again (checks that Qt and its plugins are bundled).
"""

from __future__ import annotations

import os
import sys
import tempfile
import time
import traceback
from pathlib import Path

from . import build_info


def _step(name: str, fn) -> bool:
    t = time.time()
    try:
        msg = fn()
        print(f"[ OK ] {name} ({time.time() - t:.1f} s){': ' + msg if msg else ''}")
        return True
    except Exception as e:
        print(f"[FAIL] {name}: {e}")
        traceback.print_exc(file=sys.stdout)
        return False


def daw_route() -> str:
    from .backends.daw import DawRoundtripBackend, SessionIndex
    from .core import AnalysisOptions, CaptureSettings, analyze, create_session, load_session, write_outputs
    from .core.audio_io import read_audio
    from .testing import LTIPlugin, cab_ir, simulate_render

    with tempfile.TemporaryDirectory() as d:
        d = Path(d)
        session = create_session(CaptureSettings.for_type("cabinet"))
        files = DawRoundtripBackend(SessionIndex(d / "index.json")).create_test_file(session, d / session.test_file)
        session = load_session(files[1])
        test = read_audio(files[0])
        rendered = simulate_render(LTIPlugin(cab_ir(session.sample_rate)), test.data, session.sample_rate,
                                   latency=64, stereo_out=True, name="selftest_render")
        result = analyze(session, rendered, AnalysisOptions())
        if not result.ok or result.rating != "excellent":
            raise RuntimeError(f"unexpected result: {result.rating}, {[c.message for c in result.checks]}")
        out = write_outputs(result, d)
        if not all(p.exists() for p in out.all):
            raise RuntimeError("output files missing")
        return f"null test {result.null_test.residual_db:.0f} dB"


def plugin_route() -> str:
    from .backends.plugin_host import PluginHostBackend
    from .core import CaptureSettings, create_session

    session = create_session(CaptureSettings.for_type("eq", sweep_length_s=2.0), source="plugin")
    with PluginHostBackend() as host:
        host.load("builtin:Gain:gain_db=-6")
        result = host.capture_and_analyze(session)
    if not result.ok or result.rating != "excellent":
        raise RuntimeError(f"unexpected result: {result.rating}, {[c.message for c in result.checks]}")
    return f"null test {result.null_test.residual_db:.0f} dB"


def gui() -> str:
    if sys.platform.startswith("linux") and not os.environ.get("DISPLAY") and not os.environ.get("WAYLAND_DISPLAY"):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    try:
        from PySide6.QtWidgets import QApplication
    except ImportError:
        return "skipped (PySide6 not installed)"
    from .gui.main_window import MainWindow

    app = QApplication.instance() or QApplication([])
    w = MainWindow()
    w.show()
    for _ in range(20):
        app.processEvents()
        time.sleep(0.01)
    w.close()
    return f"platform {app.platformName()}"


def run_selftest() -> int:
    print(f"irene-capture {build_info()} self-test on {sys.platform}, Python {sys.version.split()[0]}")
    ok = all([_step("DAW route", daw_route), _step("Direct plugin route", plugin_route), _step("GUI", gui)])
    print("Self-test passed." if ok else "Self-test FAILED.")
    return 0 if ok else 1
