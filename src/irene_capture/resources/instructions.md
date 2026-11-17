<!--
DAW instructions for irene-capture. Edit freely; the app reads this file at runtime.

Format:
- A line starting with "# " starts a section. The app shows the sections "General tips",
  "Cubase" and "Logic Pro" as tabs (General tips are appended to each DAW tab).
- Placeholders in braces are filled in by the app:
    {rate}         sample rate, e.g. "48 kHz"
    {rate_hz}      sample rate in Hz, e.g. "48000"
    {track}        "mono" or "stereo"
    {mono_downmix} "on" or "off"
    {test_file}    name of the test file
    {session_file} name of the session file (.capture.json)
    {length}       length of the test file, e.g. "18.2 s"
- A line starting with "?condition " is only shown if the condition is true.
  Conditions: mono, stereo, truestereo, cabinet, reverb, eq, custom, absolute (normalization off).
  Several conditions can be combined with "|" (any of them), e.g. "?cabinet|eq ".
- Comments like this one are not shown.
-->

# General tips

- **Keep the session file** `{session_file}` next to the test file. irene-capture needs it to analyze the rendered file.
?reverb - **Reverb:** set the mix to 100 % wet (unless you want the dry signal in the IR). Turn off modulation/chorus, randomization, freeze/infinite and ducking.
?cabinet - **Cab sims:** turn off noise/hum simulation. For mono captures, also turn off doubling and stereo widening.
?eq - **EQ/filter:** turn off any "auto gain", analyzer-driven dynamic bands and saturation/drive.
- **Only linear, time-invariant processing** can be captured as an IR. Amps, drive, compressors and modulation effects give poor results; irene-capture will warn you.
- **Signal path:** no plugins on the stereo out or master bus (no limiter). Track fader at 0 dB, pan centered.
?absolute - **Absolute level matters here** (normalization is off): set the pan law to 0 dB (Cubase: Project → Project Setup → Stereo Pan Law; Logic: File → Project Settings → Audio → Pan Law).

# Cubase

1. Create a new empty project. **Project → Project Setup** (Shift+S): set the **Sample Rate** to **{rate}**. Bit resolution 32-bit float is recommended.
2. Add an audio track: **{track}** (Project → Add Track → Audio, Configuration: {track}).
3. **File → Import → Audio File…** and import `{test_file}` ({length}). If Cubase offers to convert the sample rate, the project sample rate is wrong: cancel and fix it in Project Setup.
4. Insert the plugin in the track's **Inserts** and dial in the sound (see the tips below).
5. Select the event and press **P** (Locators to Selection).
6. **File → Export → Audio Mixdown…** with these settings:
   - Export range: **Locators**
   - Channel: **Stereo Out** (or the audio track itself, if Channel Batch Export is available)
   - File type: **Wave**, sample rate = project ({rate}), bit depth **32-bit float**
   - **Mono Downmix: {mono_downmix}**
?absolute   - With Mono Downmix, Cubase applies the stereo pan law; with the pan law at 0 dB the level stays exact.
   - **Real-Time Export: off**
   Choose a name and folder, then click **Export Audio**.
7. *Alternative:* **Edit → Render in Place** (Render Settings: "Channel Settings", tail 0 — the test file already contains the tail). The file ends up in the project's Audio folder.
8. Import the exported file into irene-capture ("Import rendered file(s)…" or drag and drop).

# Logic Pro

1. Create a new empty project. **File → Project Settings → Audio:** set the **Sample Rate** to **{rate}**.
2. Create an audio track: **{track}**.
3. Drag `{test_file}` onto the track **exactly at bar 1** (zoom in to check). If Logic asks to convert the sample rate, the project sample rate is wrong: cancel and fix it.
4. Insert the plugin in an **Audio FX** slot (see the tips below).
5. Select the region and press **Cmd+U** (Set Locators by Region). The rounding to whole bars is fine; extra silence at the end is tolerated.
6. **File → Bounce → Project or Section** (Cmd+B) with these settings:
   - Destination: **PCM**
   - Mode: **Offline**
   - Normalize: **Off**
   - Format: **WAVE**, resolution **32-bit float**, sample rate = project ({rate})
   - File type: **Interleaved**
   - Dither: **None**
   - "Include Audio Tail" can stay on
?mono   Logic bounces in stereo unless the output channel strip is mono; irene-capture converts identical channels to mono automatically.
7. *Alternative:* **Bounce in Place** (Ctrl+B) with Normalize **off**. The file ends up in the project's Audio Files folder.
8. Import the bounced file into irene-capture ("Import rendered file(s)…" or drag and drop).
