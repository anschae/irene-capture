# irene-capture

Capture **impulse responses (IRs)** from audio plugins — guitar cabinet sims, reverbs,
EQs and filters — and load them into [IRene](https://github.com/anschae/irene) or any
other IR loader.

Two ways to capture:

1. **Render in DAW** — irene-capture creates a test file; you render it through the plugin
   in Cubase or Logic Pro and import the result.
2. **Use plugin directly** — irene-capture loads a VST3 (or AU on macOS) plugin itself, shows
   its editor and renders the test signal through it. No DAW needed.

Both routes run the same analysis: plain-language checks of the rendered file, a
deconvolution that is exact to better than −80 dB, automatic latency and tail trimming,
normalization, and a **null test** that tells you how well the IR reproduces the plugin.

> **Only linear, time-invariant (LTI) processing can be captured as an IR.**
> Cabinet sims, reverbs without modulation, EQs and filters work.
> Amps, drive, compressors and modulation effects (chorus, modulated reverbs) don't — an IR
> can't reproduce them. irene-capture measures the distortion, runs the null test and tells
> you when that's the case.

## Download

No installation and no Python needed.

* **Releases:** <https://github.com/anschae/irene-capture/releases> (when a version is tagged)
* **Latest build:** open [Actions](https://github.com/anschae/irene-capture/actions/workflows/ci.yml),
  click the newest green run of `main`, scroll down to **Artifacts**:

| file | for |
|---|---|
| `irene-capture-<version>-windows.zip` | Windows 10/11 (64-bit): `irene-capture.exe` |
| `irene-capture-<version>-macos-arm64.zip` | Macs with Apple silicon (M1 and later): `irene-capture.app` |
| `irene-capture-<version>-macos-intel.zip` | Intel Macs |
| `irene-capture-<version>-linux.zip` | Linux x86-64 (single binary) |

Unzip and start it. The Windows `.exe` unpacks itself on start, which takes a few seconds.

### First launch of the unsigned builds

The builds are not signed with a paid Apple/Microsoft certificate, so the system warns once.

**macOS** — use one of these:

* Right-click (or Control-click) `irene-capture.app` → **Open** → **Open**.
* Or try to open it, then **System Settings → Privacy & Security** → scroll down →
  **Open Anyway**.
* Or in Terminal: `xattr -dr com.apple.quarantine /path/to/irene-capture.app`

**Windows SmartScreen** — "Windows protected your PC": click **More info** → **Run anyway**.

## User guide

### 1. Setup

| setting | meaning |
|---|---|
| **Capture type** | a preset with sensible defaults (table below). Every value stays editable. |
| **Method** | *Exponential sine sweep* (default; also measures distortion) or *Dirac impulse*. |
| **Sample rate** | 44.1 / 48 / 88.2 / 96 / 192 kHz. The DAW project must use the same rate. |
| **IR length** | how long the IR may be. A warning appears if the value is unusual for the type. |
| **Channels** | reverb and custom: *Stereo* (mono in → stereo out), *True stereo* or *Mono*. |
| **Output folder** | where test files are suggested and direct-route IRs are written. |

| type | channels | IR length | trim latency | normalization |
|---|---|---|---|---|
| Cabinet | mono | 200 ms | on | energy |
| Reverb | stereo | 4 s | off (keeps pre-delay) | energy |
| EQ / Filter | mono | 250 ms | off (linear-phase EQs pre-ring) | off (absolute gain) |
| Custom | mono/stereo | 1 s | your choice | your choice |

**Advanced:** sweep length (6 s) and range (20 Hz – 0.95 × Nyquist), test level (−6 dBFS),
test-file format (32-bit float recommended), IR format (24-bit or 32-bit float), trim
latency, automatic tail trim with fade-out, normalization (off / peak / energy).
Settings and folders are remembered.

**True stereo** plays the test sequence on the left channel only, then on the right only,
and produces four IRs (L→L, L→R, R→L, R→R): either **two stereo files**
`…_ir_inL.wav` and `…_ir_inR.wav` (default) or **one 4-channel WAV**.

### 2a. Capture: Render in DAW

1. Click **Create test file…** and choose where to save it. Next to it, irene-capture writes
   `<test file>.capture.json` (the **session file**) and `<test file>_instructions.txt`.
   **Keep the session file next to the test file** — DAWs remove metadata from WAV files,
   so the app needs it to analyze the render.
2. Follow the step-by-step instructions shown in the app (tabs for **Cubase** and
   **Logic Pro**, with a Copy button). They are the same as in
   [`resources/instructions.md`](src/irene_capture/resources/instructions.md), filled in with
   your sample rate and channel setup. In short:
   * new project at the session's sample rate, a mono (cabinet/EQ) or stereo (reverb) track,
   * import the test file at the very start, insert the plugin,
   * export exactly the event/region (Cubase: select it, **P**, *Audio Mixdown*, 32-bit float,
     Mono Downmix for mono captures; Logic: **Cmd+U**, *Bounce Project or Section*, Offline,
     Normalize off, 32-bit float) — or use Render/Bounce in Place.
   * Tips: reverbs 100 % wet without modulation; cab sims without noise; nothing on the
     master bus; fader at 0 dB; for EQs with normalization off set the pan law to 0 dB.
3. **Import rendered file(s)…** or drag them onto the window. Several files of the same
   session (e.g. several mic positions) are analyzed as a batch.

The session is found by the 4-character ID in the file name (`irc_reverb_sweep_48k_st_7f3a…`),
otherwise the most recent session is used (you're told), otherwise you're asked for the JSON.

### 2b. Capture: Use plugin directly

1. Pick a plugin from the list (standard folders are scanned; **Add folder…** for others,
   **Browse…** for a single file) and **Load** it.
2. **Open editor** and dial in the sound. If a plugin's editor can't be shown, use
   **Generic controls…** (sliders for all parameters).
3. Name the capture and press **Capture** (an open editor is closed first).
4. Change the sound and capture again as often as you like — no reloading needed.

The plugin runs in a separate process, so a crashing plugin can't take the app down (it is
loaded again on the next action, with its default settings). If a plugin's window doesn't
close when you press Capture, close it yourself — the capture starts then.
Before capturing, the plugin is reset and warmed up with 1 s of silence, its reported
latency is compensated, and the test signal is processed twice to check that the plugin
is deterministic. Shell plugins (like WaveShell) can't be loaded directly — use the DAW route.
If the plugin exposes its state, it is saved next to the IR (`…_plugin.vstpreset` for VST3,
`…_plugin_state.bin`), and you can optionally save the rendered audio.

### 3. Results

Before any file is written you see a **summary**: every check with ✓ / i / ! / ✕ and what to
do, every **conversion** the app made (e.g. "converted to mono", "latency trimmed",
"normalized"), the null-test **rating**, and plots: waveform, frequency response (with
1/6-octave smoothing), energy decay with RT60 (reverbs), and the null-test error spectrum.
Then **Create IR files**. Errors (wrong sample rate, clipped or silent file, …) block it.

Written next to the rendered file (DAW route) or into the output folder (direct route),
never overwriting anything (`_2`, `_3`, … are appended):

* `<name>_ir.wav` — the IR (24-bit by default; 32-bit float if it would clip)
* `<name>_ir.json` — everything: settings, session ID, checks, conversions, offset, trimmed
  latency, normalization gain, tail, distortion, null test, rating, tool version
* `<name>_ir.txt` — a short readable report

### What the checks mean

| check | result |
|---|---|
| sample rate differs from the session | **error** — set the DAW project rate, render again (no resampling) |
| 16-bit file | warning (reduced quality); 24/32-bit int: info (converted to float) |
| stereo file for a mono capture | identical channels → converted to mono; different → you choose L / R / sum |
| mono file for a stereo capture | warning, continue with a mono IR or cancel |
| file too short | **error**; if only the verification part is missing: warning, no null test |
| silent / unprocessed | **error** — wrong track exported, plugin muted or bypassed? |
| clipping | integer file: **error** (lower the test level); float > 0 dBFS: warning |
| offset | small = latency/pre-delay (kept or trimmed); export started early/late → realigned; beginning missing → **error** |
| non-linearity (sweep) | warning above 1 % THD: the IR captures only the linear part |
| IR truncated | warning — increase the IR length |

### Null test ratings

| null | rating |
|---|---|
| ≤ −60 dB | excellent |
| −60 … −40 dB | very good |
| −40 … −25 dB | audible differences possible |
| > −25 dB | unusable |

High distortion → the plugin is non-linear. Low distortion but a high residual → the
plugin is time-variant (modulation, randomization), the IR was truncated, or misaligned.
[docs/DSP.md](docs/DSP.md) explains all of this in detail.

### Known limitations of the direct route

* Tested in CI with real VST3s (Dragonfly Reverb, Surge XT Effects) on Windows, macOS and
  Linux. AUs on macOS still need a real-world test in your plugin folder.
* On Linux, some plugin editors crash under X11 without a full desktop; use **Generic controls…**.
* Instruments, plugins that need a running transport/tempo, sidechain or shell plugins
  (WaveShell) can't be captured directly — use the DAW route.

## IR formats and IRene

IRene loads **mono and stereo WAV/AIFF files of up to 10 s** (longer files are cut; files
with more than two channels use the first two). Therefore irene-capture's defaults are:

* mono captures → mono WAV; stereo captures → stereo WAV;
* true stereo → **two stereo files** (`_ir_inL.wav`, `_ir_inR.wav`). A 4-channel file can be
  chosen, but IRene would only use its first two channels (L→L, L→R). IRene has no
  true-stereo mode yet; load the two files into two slots for a partial true-stereo setup.
* IRs longer than 10 s get an info message.

## Command line

The same executable has a CLI (on Windows, output appears in the console it was started from):

```sh
irene-capture generate --type reverb --rate 48000 --out ~/captures/
irene-capture analyze ~/captures/irc_reverb_sweep_48k_st_7f3a_bounce.wav
irene-capture plugin-capture --plugin "/Library/Audio/Plug-Ins/VST3/Some Reverb.vst3" --type reverb --editor
irene-capture --selftest
irene-capture --help
```

## Building from source

```sh
python3.12 -m venv .venv && . .venv/bin/activate
pip install -e ".[gui,dev]"
pytest                    # tests
irene-capture             # GUI
pyinstaller packaging/irene-capture.spec --noconfirm   # executable in dist/
```

See [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) for the code structure and
[CLAUDE.md](CLAUDE.md) for development notes.

## License

GPL-3.0-or-later (see [LICENSE](LICENSE)); plugin hosting uses Spotify's
[pedalboard](https://github.com/spotify/pedalboard) (GPLv3), the GUI uses Qt for Python
(LGPL) and pyqtgraph (MIT).
