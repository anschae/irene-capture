# irene-capture: notes for whoever works on this next

Standalone desktop app (Python 3.12, PySide6, numpy/scipy, pedalboard, PyInstaller) that
captures impulse responses from plugins, the companion to the IR loader
[IRene](https://github.com/anschae/irene). The owner is a teacher and guitarist, not a
developer: he has no local toolchain and gets all builds from GitHub Actions. Explain things
in plain language. The original specification is summarized in README.md; decisions that go
beyond it are listed below. **Keep this file current with every significant change.**

## Commands

```sh
python3.12 -m venv .venv && . .venv/bin/activate
pip install -e ".[gui,dev]"
ruff check src tests
pytest -q                                   # ~30 s; GUI test runs offscreen
QT_QPA_PLATFORM=offscreen irene-capture --selftest
pyinstaller packaging/irene-capture.spec --noconfirm   # → dist/
IRENE_SCREENSHOTS=/tmp/shots pytest tests/test_gui.py  # PNGs of the pages (look at the UI)
```

Watch CI: `gh run list --repo anschae/irene-capture`, `gh run view <id> --log-failed`
(works without a token for this public repo; `GH_TOKEN=` if the session's token is invalid).

## Layout

See docs/ARCHITECTURE.md (modules, data flow, how to add the hardware backend) and
docs/DSP.md (signal design and maths, written for the owner). In one line each:

* `core/` pure DSP and data model, no Qt, no devices. `analyze()` in `core/analysis.py` is
  the single pipeline for every route. Checks return `CheckResult`s; user choices are
  requested by raising `NeedsDecision` and answered via `AnalysisOptions.decisions`.
* `backends/` where audio comes from: `daw.py` (test file, instructions, session lookup),
  `plugin_host.py` (pedalboard in a spawned worker process), `hardware.py` (v2 stub).
* `gui/` PySide6 + pyqtgraph. `cli.py`, `selftest.py`. `testing.py` has synthetic plugins.
* `resources/instructions.md` — the DAW instructions (editable; placeholder/condition syntax
  is documented at its top). `resources/icon.png` is drawn by `tools/make_icon.py`.

## Rules

* **Don't change the signal design silently.** Sessions are regenerated from their JSON; if
  `compute_layout`, the sweep or the noise generator changes, old session files would no
  longer match their test files. Bump `SCHEMA_VERSION` and keep the old path if needed.
  `Session.from_dict` refuses files whose layout doesn't match.
* The pink noise must stay on `PCG64(seed).random_raw` (NumPy guarantees the bit stream, not
  the distributions).
* The core must not import Qt; the plugin worker must not import Qt.
* Every user-facing message: a short sentence + a detail that says what to do.
* `multiprocessing.freeze_support()` stays the first call in `__main__.main` and
  `packaging/launcher.py`.
* Version lives only in `src/irene_capture/_version.py`. A tag `vX.Y.Z` must match it (CI
  checks) and creates a GitHub Release with the zips.

## Decisions made (beyond the spec)

* **Deconvolution = Wiener-regularized spectral division** (not the Farina inverse filter):
  `H = Y·X*/(|X|² + |N|²)`, with |N|² measured in the pre-roll plus the file format's
  quantization noise. Exact where the sweep is above the noise, no band-limit artifacts for
  clean renders. An earlier version with a fixed zero-phase band limit (ε rising below
  20 Hz) cut off the band limit's own pre-ringing at t = 0 and gave nulls of only −28 dB.
* **Above the sweep band** (top 1/12 octave of the sweep is not trusted, then up to
  Nyquist): if H looks like a delay there (flat, linear phase), it is continued smoothly
  (gains/delays/EQs become exact, no pre-ringing); otherwise tapered to zero.
* **Verification noise and null test use the same trusted band** (20 Hz … 2^(−1/12)·f_end).
  Outside it the IR is undefined.
* **Onset** = first sample above −40 dB re peak on a Gaussian-smoothed (σ 1.5 samples) copy.
  Trim latency keeps 0.5 ms before the onset. Without trim the IR starts at zero delay,
  except when there's content before it connected to the onset (down to −80 dB / noise,
  gaps ≤ 2 ms): then it is kept (linear-phase pre-ringing).
* **Alignment:** lag > pre-roll → realign (drop samples); lag < −1 ms (file starts late) →
  realign (pad); lag < −pre-roll → error. Latency/pre-delay is reported from the IR onset,
  not from the cross-correlation peak (which for reverbs lands somewhere in the tail).
* **Tail:** smoothed energy envelope (10 ms); end where it drops below noise + 6 dB or
  −100 dB re peak, + 20 ms; half-Hann fade of 10 % (5–200 ms). Noise floor from the
  pre-roll mapped through the deconvolution filter. Truncated = end level above −60 dB and
  noise + 10 dB. Not reported for strongly non-linear plugins (the distortion products
  cover the tail).
* **Energy normalization:** mean energy per IR channel = 1, one gain for all channels.
* **24-bit IR that would clip** is written as float instead (warning).
* **True stereo default output: two stereo files**, because IRene uses only the first two
  channels of a 4-channel file.
* **Never overwrite:** `_2`, `_3`… are appended (no question asked); the JSON/TXT/attachments
  share the stem.
* **Session lookup** also searches the parent folder of the rendered file, the last
  test-file folder and an index of recent sessions in the config folder
  (`IRENE_CAPTURE_CONFIG_DIR` overrides it; the tests use that).
* **Direct route processing:** one `process()` call with reset, 1 s silence warm-up, then the
  signal (pedalboard compensates reported latency). Mono captures try a mono bus first and
  fall back to stereo (L = R). Determinism: two runs, warning above −100 dB difference.
* **Plugin state** is captured right after the capture (so later tweaks don't change it)
  and written next to the IR: `_plugin.vstpreset` (VST3 `preset_data`) and
  `_plugin_state.bin` (`raw_state`).
* **Windows .exe is windowed**; CLI output is shown by attaching to the parent console.
  CI uses `--log` because a windowed exe started from PowerShell has no stdout.
* **Artifacts** are uploaded with `upload-artifact@v7` and `archive: false`, so the download
  is the zip itself (no zip-in-zip). macOS: `.app` ad-hoc signed, zipped with `ditto`.
* **macOS Intel** builds on `macos-15-intel` (GitHub's last Intel image, supported until
  fall 2027). Linux builds on ubuntu-22.04 for an older glibc.
* Instructions: Logic only bounces stereo when the output is stereo (Apple's manual says a
  mono output gives a mono file), so the text says so.

## Spike: plugin editor + PySide6 + PyInstaller

The CI job `plugin-spike` (non-blocking) runs the **frozen** app on Windows, macOS arm64 and
Linux (Xvfb): it downloads Dragonfly Reverb (GPL VST3), opens the editor in the worker
process, closes it automatically after a few seconds (`spike-editor`), then captures with
`plugin-capture --editor-seconds 3`. On macOS it also loads Apple's built-in AU
`AULowpass` from `/System/Library/Components/CoreAudio.component`.

Results: see "Status" below (update after each run).

## Status

* Done: core, CLI, both routes, GUI, tests (68), CI on 3 OSes, builds for Windows,
  macOS arm64 + Intel, Linux, self-test of every build.
* Spike results: _pending first run_.
* Not yet tested by a person in Cubase/Logic or with real third-party plugins on a desktop.

## Known issues / ideas

* The plugin editor window opens from a separate process, so it may appear behind the main
  window; it has its own close button.
* Plugins that need a running host transport/tempo or sidechain can't be captured directly.
* IRene has no true-stereo mode; a separate task in the IRene repo if wanted.
* Hardware capture (v2): see docs/ARCHITECTURE.md.
