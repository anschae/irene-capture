"""GUI smoke test (offscreen): DAW route end to end without dialogs."""

import os
import time

import pytest

pytest.importorskip("PySide6")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QSettings  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from irene_capture import testing as T  # noqa: E402
from irene_capture.core import create_session  # noqa: E402
from irene_capture.core.audio_io import read_audio, write_audio  # noqa: E402
from irene_capture.core.presets import BitDepth  # noqa: E402


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


def wait_for(app, cond, timeout=60):
    t = time.time()
    while not cond():
        app.processEvents()
        time.sleep(0.01)
        if time.time() - t > timeout:
            raise TimeoutError
    app.processEvents()


def test_daw_route(app, tmp_path):
    from irene_capture.gui.main_window import MainWindow

    qs = QSettings(str(tmp_path / "settings.ini"), QSettings.Format.IniFormat)
    w = MainWindow(qs)
    w.show()
    w.setup.out_dir.setText(str(tmp_path))
    w.setup.type.setCurrentIndex(w.setup.type.findData("reverb"))
    assert w.setup.length.value() == 4.0 and w.setup.stereo.currentData() == "stereo"
    w.setup.length.setValue(0.2)
    assert "very short for a reverb" in w.setup.length_msg.text()
    w.setup.length.setValue(2.5)
    w.setup.sweep_len.setValue(2.0)
    s = create_session(w.setup.settings())
    daw = w.capture.daw
    wav, js, _ = daw.backend.create_test_file(s, tmp_path / s.test_file)
    daw.instructions.show_for(s)
    daw.middle.setCurrentIndex(1)
    assert daw.instructions.tabs.count() == 2
    x = read_audio(wav).data
    r = T.simulate_render(T.LTIPlugin(T.reverb_ir(length_s=2.0, rt60_s=0.6)), x, s.sample_rate)
    rendered = tmp_path / (wav.stem + "_render.wav")
    write_audio(rendered, r.data, s.sample_rate, BitDepth.FLOAT32)
    daw.import_files([rendered])
    wait_for(app, lambda: w.results.results)
    res = w.results.results[0]
    assert res.rating == "excellent" and w.stack.currentIndex() == 2
    assert "Checks" in w.results.checks.toHtml()
    w.results.create_files()
    assert res.written and all(p.exists() for p in res.written)
    if os.environ.get("IRENE_SCREENSHOTS"):
        out = os.environ["IRENE_SCREENSHOTS"]
        w.capture.tabs.setCurrentIndex(1)
        w.go(1)
        app.processEvents()
        w.grab().save(f"{out}/plugin_tab.png")
        w.capture.tabs.setCurrentIndex(0)
        for i, name in enumerate(("setup", "capture", "results")):
            w.go(i)
            app.processEvents()
            w.grab().save(f"{out}/{name}.png")
        for i in range(4):
            w.results.plots.setCurrentIndex(i)
            app.processEvents()
            w.grab().save(f"{out}/results_plot{i}.png")
    w.close()


def test_plugin_route(app, tmp_path):
    from irene_capture.gui.main_window import MainWindow

    qs = QSettings(str(tmp_path / "settings.ini"), QSettings.Format.IniFormat)
    w = MainWindow(qs)
    w.show()
    w.setup.out_dir.setText(str(tmp_path))
    w.setup.type.setCurrentIndex(w.setup.type.findData("eq"))
    w.setup.sweep_len.setValue(2.0)
    tab = w.capture.plugin
    tab.load("builtin:Gain:gain_db=-6")
    wait_for(app, lambda: tab.capture_btn.isEnabled())
    assert "Gain" in tab.loaded.text()
    tab.name.setText("minus six")
    tab.save_rendered.setChecked(True)
    tab.capture()
    wait_for(app, lambda: w.results.results)
    res = w.results.results[0]
    assert res.rating == "excellent" and res.plugin_info["name"] == "Gain"
    w.results.create_files()
    names = sorted(p.name for p in res.written)
    assert "Gain_minus_six_ir.wav" in names and "Gain_minus_six_ir_rendered.wav" in names
    w.close()
