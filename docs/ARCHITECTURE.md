# Architecture

```
            ┌────────────── GUI (PySide6) ───────────────┐      ┌──── CLI ────┐
            │ Setup  →  Capture (DAW tab | plugin tab) → Results │      │ generate     │
            └───────────────┬─────────────────────────────┘      │ analyze      │
                            │                                    │ plugin-capture│
                            ▼                                    └──────┬───────┘
     ┌──────────────── backends ─────────────────┐                     │
     │ DawRoundtripBackend   PluginHostBackend    HardwareBackend (v2) │◄────┘
     │  test file + JSON      pedalboard in a      stub, hidden         │
     │  import rendered file  worker process                           │
     └───────────┬──────────────────┬────────────────────────────────────┘
                 │ RenderedAudio     │ RenderedAudio (+ determinism check)
                 ▼                   ▼
     ┌──────────────────────── core (pure DSP) ──────────────────────────┐
     │ presets → session (layout, JSON) → signals (test file)            │
     │ analyze(): checks → alignment → deconvolution → postprocess →     │
     │            nulltest → CaptureResult      report.write_outputs()   │
     └───────────────────────────────────────────────────────────────────┘
```

## Packages

| module | what |
|---|---|
| `core/presets.py` | capture types, `CaptureSettings`, validation messages |
| `core/session.py` | `Session`, `Layout` (sample offsets), `HardwareSetup` (v2 fields), versioned JSON |
| `core/signals.py` | sweep, Dirac, pink noise, full test file (deterministic) |
| `core/audio_io.py` | read/write WAV/AIFF/FLAC, `RenderedAudio`, never-overwrite paths |
| `core/checks.py` | `CheckResult(severity, message, detail, conversion, decision)` and the format checks |
| `core/deconvolution.py` | Wiener-regularized sweep deconvolution, Dirac, distortion estimate |
| `core/postprocess.py` | onset, pre-ringing, tail detection, fades, normalization |
| `core/nulltest.py` | prediction, residual, rating, bands, error spectrum |
| `core/metrics.py` | magnitude response with smoothing, EDC, RT60 (for plots/report) |
| `core/analysis.py` | `analyze()` — the pipeline every route uses |
| `core/report.py` | IR files, `_ir.json`, `_ir.txt`, attachments |
| `backends/daw.py` | test file creation, DAW instructions (`resources/instructions.md`), session lookup |
| `backends/plugin_host.py` | worker process, plugin scanning, editor, generic parameters, state |
| `backends/hardware.py` | v2 stub |
| `gui/` | main window, pages, plots, background runner |
| `cli.py`, `selftest.py` | command line, `--selftest` |
| `testing.py` | synthetic plugins and simulated renders (tests + self-test) |

The core never imports Qt or touches audio devices. The worker process of the plugin host
never imports Qt either.

## Data flow of one capture

1. `create_session(settings)` → `Session` with a fresh ID and the sample layout.
2. A backend produces `RenderedAudio` (float64 frames × channels, sample rate, subtype).
3. `analyze(session, rendered, AnalysisOptions)` → `CaptureResult` with the IR
   (inputs × outputs × samples), all `CheckResult`s, metrics and null tests. If a check
   needs a choice (which channel? accept mono?), `NeedsDecision` is raised; the caller asks
   the user, puts the answer into `options.decisions` and calls `analyze` again.
4. The GUI shows the summary; only then `write_outputs(result, folder)` writes files.

## Decisions as data

Checks never print; they return objects with a severity (`ok/info/warning/error`), a short
message, a detail that says what to do, a `conversion` flag (the app changed the audio) and
optionally a `Decision`. GUI, CLI and reports render the same objects.

## The plugin host

`PluginHostBackend` starts `worker_main` with `multiprocessing` (`spawn`). Two one-way
pipes carry commands and replies. The worker's main thread runs the commands, including
`show_editor`, which blocks until the window closes; a reader thread keeps receiving and
handles `close_editor` by setting the editor's close event. If the worker dies, the next
call raises `PluginHostCrashed` and a new worker is started on the next command.
`multiprocessing.freeze_support()` is the first thing the frozen app runs.

Processing: `reset` + 1 s of silence (warm-up) + test signal in one `process()` call
(pedalboard compensates the reported latency), then the warm-up is cut off. The signal is
processed twice to check determinism.

## Adding a backend (hardware, v2)

The hardware route only needs a new `CaptureBackend` that returns `RenderedAudio`; the
analysis stays the same. What it will need:

* **Audio I/O**, e.g. with `sounddevice` (PortAudio): play the test file on an output, record
  on an input, same sample rate, ideally full duplex in one stream.
* **Latency calibration.** Interfaces add round-trip latency. Either a loopback cable on a
  second input (`HardwareSetup.loopback_channel`): cross-correlate the loopback with the dry
  signal and shift the recording; or a one-time calibration measurement stored per device.
  Without it, the IR contains the interface latency (trim latency handles cabinets).
* **Averaging.** `HardwareSetup.repetitions`: play the capture signal several times and
  average the aligned recordings (+3 dB SNR per doubling). The layout then needs repeated
  capture blocks; `compute_layout` is the one place to change, and `SCHEMA_VERSION` must go up.
* **Longer sweeps for SNR** (the 30 s maximum is already allowed) and a check that the
  pre-roll noise floor is low enough (already measured: `CaptureResult.noise_floor_dbfs`).
  The Wiener deconvolution already uses the measured noise spectrum, so noisy recordings
  degrade gracefully.
* **Level calibration.** `HardwareSetup.output_gain_db`/`input_gain_db`/
  `level_calibration_dbfs`, so that "normalization off" can mean a known absolute gain
  through the hardware, and a clipping/low-level check on the input.
* **GUI:** a third tab on the Capture page (device pickers, input meter, test tone). The
  backend class has `available = False` today, which keeps it hidden.
* **Non-linearity is normal for hardware** (speakers, amps): keep the distortion estimate as
  information and use the sweep method by default.
