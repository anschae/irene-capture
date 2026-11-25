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
  gaps ≤ 2 ms): then it is kept (linear-phase pre-ringing) and the IR gets longer by that
  amount (the window is [start, IR length), not shifted). Skipped for strongly non-linear
  plugins, whose "content before zero" is distortion products.
* **Alignment:** lag > pre-roll → realign (drop samples); lag < −1 ms (file starts late) →
  realign (pad); lag < −pre-roll → error. Latency/pre-delay is reported from the IR onset,
  not from the cross-correlation peak (which for reverbs lands somewhere in the tail).
* **Tail:** smoothed energy envelope (10 ms); end where it drops below noise + 6 dB or
  −100 dB re peak, + 20 ms; half-Hann fade of 10 % (5–200 ms). Noise floor from the
  pre-roll mapped through the deconvolution filter. Truncated = end level above −60 dB and
  noise + 10 dB. Not reported for strongly non-linear plugins (the distortion products
  cover the tail).
* **Cabinet preset: tail trim off by default** (owner's wish, 25 Nov 2026: a 200 ms cab IR
  came out as 46 ms, which was surprising; fixed-length cab IRs are easier to compare and
  blend in IRene). Reverb/EQ/Custom keep tail trim on. The setup page says "IR length
  (max.)" and shows whether the IR may end earlier.
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

The build job runs a non-blocking spike on the **frozen** app right after building
(Windows, macOS arm64, Linux under Xvfb + openbox): it downloads Dragonfly Reverb (GPL,
DPF-based) and on Windows also Surge XT Effects (JUCE), opens each editor in the worker
process, closes it after a few seconds (`spike-editor`), then captures with
`plugin-capture --editor-seconds 3`. `tools/vst3_probe.py` loads the same plugin with plain
pedalboard for comparison. Results are written to the job summary ("SPIKE RESULTS").

Results (Nov 2026, pedalboard 0.9.26, PySide6 6.x, PyInstaller 6.x):

| | Windows | macOS arm64 | Linux (Xvfb) |
|---|---|---|---|
| worker process (`spawn`) in the frozen app | ✓ | ✓ | ✓ |
| VST3 load | ✓ after fix ¹ | ✓ | ✓ |
| editor opens and closes (close event) | ✓ Dragonfly ×2, Surge XT | ✓ Room; Early Reflections hangs ² | ✗ DPF editor crashes (X BadWindow) ³ |
| capture through the plugin | ✓ | ✓ | ✓ |
| Dragonfly Early Reflections (LTI) null test | −68.7 dB excellent | (see ²) | −68.7 dB excellent |
| Dragonfly Room (has modulation) | flagged time-variant, −23 dB | same | same |
| Apple AU (`AULowpass` in CoreAudio.component) | – | ✗ plain pedalboard hangs too ⁴ | – |

1. pedalboard can't scan VST3 *bundle folders* on Windows ("unsupported plugin format or
   scan failure") but loads the module inside: `resolve_plugin_path()` now does that.
2. Loads fine; `show_editor(close_event)` then never returns for this one plugin on the
   macOS runner (the close event is ignored, or the window never comes up headless). The
   GUI therefore waits for the editor to be closed before capturing, and after 6 s offers
   "Stop the plugin" (kills the worker; the plugin is reloaded with defaults). Needs a test
   on a real Mac.
3. Linux is optional. The crash is contained in the worker; the host reloads the plugin and
   the capture still works. Use "Generic controls…" on Linux.
4. Apple's system AUs hang in pedalboard on the headless CI runner; third-party AUs are
   untested in CI → ask the owner to try a real AU in Logic's plugin folder.

The plugin host survives crashes (reloads the plugin on the next action) and kills workers
that don't answer within the timeout.

**Owner's first test on Windows 10 (17 Nov 2026), and what changed:**
* Neural DSP Gojira (old version) worked. Gojira X did not finish loading within 120 s.
* Another plugin produced a PyInstaller "Unhandled exception in script" dialog from the
  worker: BrokenPipeError while sending a reply, then `sys.stderr` was None. Cause: in a
  one-file Windows build `multiprocessing.Process` is the *bootloader*; killing it (timeout)
  left the real worker running, which later wrote to the closed pipe.
* Fixes: the worker says hello with its real PID and the parent kills that PID; the parent
  closes its pipe ends, and the worker's reader thread calls `os._exit(0)` on EOF; every send
  is guarded; stdout/stderr and `faulthandler` go to `plugin-host.log` in the config folder
  (Help → Open log folder); VST3 bundles are tried as bundle first, the inner module only
  after a "scan failure"; the GUI ignores a second Load while one is running.
* Still open: why Gojira X hangs. Ask the owner for `plugin-host.log` after the next try.

## Known issues / ideas

* The plugin editor window opens from a separate process, so it may appear behind the main
  window (and on macOS the worker shows its own Dock icon while the editor is open); it has
  its own close button.
* A worker that hangs is killed after the timeout (load 120 s, state 60 s, processing 600 s).
  Config folder (sessions index, plugin-host.log): `%APPDATA%\irene-capture`,
  `~/Library/Application Support/irene-capture`, `~/.config/irene-capture`.
* Plugins that need a running host transport/tempo or sidechain can't be captured directly.
* IRene has no true-stereo mode; a separate task in the IRene repo if wanted.
* Hardware capture (v2): see docs/ARCHITECTURE.md.
