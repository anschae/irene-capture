# How irene-capture measures an impulse response

This document explains the signal processing behind irene-capture: what the test file
contains, how the impulse response (IR) is computed from the rendered file, and how the
result is checked. The code lives in `src/irene_capture/core/`.

## 1. What an impulse response can (and can't) describe

An IR describes a **linear, time-invariant (LTI)** system completely. "Linear" means:
twice the input gives twice the output, and the response to a sum is the sum of the
responses. "Time-invariant" means: the system behaves the same today as in five seconds.
For such a system the output is the input **convolved** with the IR *h*:

```
y[n] = Σ h[k] · x[n − k]            (time domain)
Y(f) = H(f) · X(f)                  (frequency domain)
```

Cabinet sims, reverbs (without modulation), EQs and filters are LTI. Amps, overdrive,
compressors (level-dependent → not linear) and chorus/flanger/modulated reverbs
(changing over time → not time-invariant) are not. irene-capture still produces an IR
for them — the best linear approximation — but warns you (distortion estimate, null test).

## 2. The test file

```
| pre-roll | capture signal | capture tail | verify signal | verify tail | post-roll |
   0.5 s     sweep 6 s        IR + 0.5 s     pink noise 4 s   IR + 0.5 s    0.5 s
```

* **Pre-roll** (0.5 s silence): time for the plugin to settle, room for small offsets,
  and the place where the **noise floor** of the render is measured.
* **Capture signal:** an exponential sine sweep (default) or a single-sample Dirac impulse.
* **Capture tail:** silence long enough for the response to die away (IR length + 0.5 s).
* **Verify signal:** 4 s of pink noise. It is *not* used to compute the IR; it is only used
  to check the IR afterwards (null test, §7).
* **True stereo:** the whole sequence runs on the left channel, then again on the right.

All signals are generated from the settings in the session file (`*.capture.json`), so the
app can regenerate the exact dry signal later — DAWs strip metadata from WAV files, which is
why the JSON is needed.

### The exponential sweep (Farina)

The sweep's frequency rises exponentially from *f₁* (20 Hz) to *f₂* (0.95 × Nyquist):

```
f(t) = f₁ · e^(t / L)          with  L = T / ln(f₂ / f₁)
x(t) = A · sin(2π f₁ L (e^(t/L) − 1))
```

It spends the same time in every octave, so its spectrum falls by 3 dB per octave (like
pink noise). Short raised-cosine fades (50 ms in, 10 ms out) avoid clicks.

Why a sweep and not just an impulse? A Dirac puts all its energy into one sample, so its
level per frequency is tiny and noise matters a lot. A 6-second sweep delivers about
6 × 48 000 times more energy — for plugins rendered in 32-bit float both are fine, but the
sweep can also **separate distortion** from the linear response (§4).

### The pink-noise verification signal

Band-limited pink noise (−3 dB/octave between 20 Hz and just below *f₂*), RMS −20 dBFS,
peak limited to the test level by scaling (never by clipping), 20 ms fades. It is made in
the frequency domain from a fixed seed with random phases. The random numbers come
directly from NumPy's PCG64 bit stream (which is guaranteed stable across versions).

## 3. Finding the start: alignment

The rendered file should start exactly where the test file starts (Render in Place,
Bounce, the direct route). To catch export mistakes the app cross-correlates the rendered
file with the dry test file (FFT correlation) and takes the lag of the largest peak:

| lag | interpretation | action |
|---|---|---|
| between −0.5 s and +0.5 s | latency or pre-delay | kept; see "latency trim" |
| > +0.5 s | export started too early | realigned automatically (info) |
| −0.5 s … −1 ms | file starts late | realigned (zeros added in front) |
| < −0.5 s | beginning missing | **error** |

## 4. Deconvolution

### Sweep: regularized spectral division

With *X* the spectrum of the dry capture region and *Y* the spectrum of the rendered one
(both zero-padded to twice the length, so the result does not wrap around):

```
            Y(f) · X*(f)
H(f) = ──────────────────────────
        |X(f)|² + |N(f)|² + ε
```

*|N(f)|²* is the noise in the recording: measured in the pre-roll (Welch's method) plus
the quantization noise of the file format (32-bit float: −144 dB re signal, 24-bit:
−144 dBFS, 16-bit: −96 dBFS). This is a **Wiener filter**:

* where the sweep is far above the noise, *H = Y / X* exactly;
* where the sweep has no energy (below 20 Hz, above *f₂*) the division would amplify
  noise, and the noise term smoothly turns it off.

For clean digital renders this recovers the plugin's IR to better than −80 dB.

**Above the sweep.** Between *f₂* and Nyquist there is no test signal, so the IR is
unknown there. Cutting it off sharply would make the IR ring at almost Nyquist —
*before* its first sample, too (a zero-phase band limit is symmetric). Two cases:

* If the response just below *f₂* looks like a plain delay (flat magnitude, linear
  phase — typical for gains, delays, most EQs), it is **continued** smoothly up to Nyquist.
  A pure gain then gives an exact single-sample IR.
* Otherwise (e.g. reverbs) it is **tapered** to zero with a raised cosine; the ringing is
  short and lies around the response, not before it.

To be safe, the top 1/12 octave of the sweep (where its fade-out is) is not trusted; the
verification noise and the null test stop there too.

### Where the IR is: time zero and negative time

Dry and rendered regions share the same time origin, so sample 0 of the result is the
**zero-delay position**: an IR that starts there has no latency. Content at *negative*
time wraps around to the end of the FFT buffer. Two kinds of things live there:

1. **Harmonic distortion.** The *k*-th harmonic of the sweep appears like a separate IR at
   time −*L*·ln *k* (for a 6 s sweep: 2nd harmonic at −0.59 s, 3rd at −0.94 s, …). The IR
   window never reaches that far: at most half-way to the 2nd harmonic.
2. **Pre-ringing** of linear-phase filters (whose latency the DAW compensates), or a file
   that starts slightly late. These are kept (see §5).

```
   3rd harm.   2nd harm.            pre-ring | main IR ...........  tail
 ─────▲───────────▲──────────────────────┬──|▲▲▲▲▲▲▲▲▲▲▲▲▲▲▲▲▲▲▲▲▲─────────▶ t
   −L·ln3      −L·ln2                 −½L·ln2  0
```

### Distortion estimate (THD)

With spectral division, the blob of harmonic *k* has a spectrum whose value at output
frequency *k·f* is the relative amplitude of that harmonic for an input tone at *f*. The
app measures harmonics 2–5 in 1/3-octave bands, and reports

```
THD = √( Σ_f Σ_k |H_k(k·f)|²  /  Σ_f |H₁(f)|² )
```

(an energy-weighted average over frequency). Above 1 % the app warns that the plugin is
non-linear; 0.1–1 % is reported as information.

### Dirac

The rendered samples starting at the impulse position, divided by the impulse amplitude.
Exact for noise-free renders, but it can't measure distortion.

## 5. Post-processing (in this order)

**Start of the IR.** The *onset* is the first sample above −40 dB re peak, measured on a
lightly smoothed copy (a Gaussian of σ = 1.5 samples removes near-Nyquist ringing).

* *Trim latency on* (cabinets): the IR starts 0.5 ms before the onset. The same start is
  used for all channels, so the timing between channels is kept.
* *Trim latency off* (reverbs keep their pre-delay, EQs keep zero latency): the IR starts at
  the zero-delay position …
* … unless there is real content before it: walking back from the onset, the app keeps
  everything above −80 dB (or the noise floor) that is connected to the onset (gaps of at
  most 2 ms). A linear-phase EQ's pre-ringing is therefore kept completely; the IR is then
  delayed by that amount compared with the plugin, which is physically unavoidable.

**Tail.** The decay is followed with a smoothed energy envelope (10 ms window, mean over
channels). The IR ends where the envelope falls below the noise floor + 6 dB, or below
−100 dB re its peak for clean renders, plus 20 ms. A half-Hann fade-out (10 % of the length,
5–200 ms) follows. The IR never gets longer than the chosen IR length.

The IR-domain noise floor is computed from the pre-roll noise: for white noise of power
σ² in the recording, the noise in the IR is σ² · Σ|G(f)|² · n / N², where *G* is the
deconvolution filter. (The pre-roll is used rather than the end of the IR window, because
a too-short IR would hide its own tail there.)

**Truncation.** If the envelope at the end of the IR window is still above both −60 dB and
the noise floor + 10 dB, the IR was cut while still ringing: *"IR was truncated — increase
IR length."*

**Normalization.**

* *Energy:* the mean energy per IR channel is 1 (Σ h² / channels = 1). A unit-energy IR
  passes white noise at unity level. Same gain for all channels.
* *Peak:* the largest sample is set to the target (e.g. −1 dBFS).
* *Off:* absolute level, as the plugin produced it (EQ captures).

The gain is stored in the report and undone for the null test. If a 24-bit IR would clip
(peak ≥ 0 dBFS), it is written as 32-bit float instead — and you are told.

## 6. Sample formats

Everything is converted to 64-bit float. Integer files are read with the usual scaling
(full scale = 1.0). 24-bit files are written from pre-scaled integers, so writing and
reading is exact. 32-bit float is recommended for the test file and the render, because it
can't clip and adds practically no noise.

## 7. Null test

The question: *does the IR behave like the plugin on material it hasn't seen?*

```
 dry verify noise ──► convolve with IR (gain undone, latency added back) ──► prediction
 rendered verify segment ──────────────────────────────────────────────────► reality
 residual = reality − prediction          null = RMS(residual) / RMS(reality)  in dB
```

Both are limited to the trusted band first (the IR isn't defined outside it). Per channel
and overall, plus three bands (< 200 Hz, 200 Hz–5 kHz, > 5 kHz) and an error spectrum.

| null | rating |
|---|---|
| ≤ −60 dB | excellent |
| −60 … −40 dB | very good |
| −40 … −25 dB | audible differences possible |
| > −25 dB | unusable |

A second number, the null on the capture segment itself, mostly checks the numerics.

**Reading the result.** High distortion → the plugin is non-linear. Low distortion but a
high residual → the plugin is time-variant (modulation, randomization), the IR was
truncated, or the file was misaligned. On the direct route the plugin is also run twice:
different results mean it's random/time-variant.

## 8. Tested with synthetic plugins

`tests/` runs the whole chain with plugins of known behavior (`irene_capture/testing.py`):
a 4096-tap cabinet FIR, a 3 s decaying stereo noise reverb, a pure delay, a gain, a
linear-phase FIR with pre-ringing, tanh distortion and a modulated delay — with latency,
export offsets, 24-bit/float renders, identical stereo channels, extra silence and noise.
The recovered IRs match the true ones to better than −80 dB inside the sweep band; LTI
plugins reach null tests of −100 dB and better.
