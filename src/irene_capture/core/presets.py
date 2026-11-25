"""Capture types (presets), settings and validation.

A capture type only sets defaults. Every value in :class:`CaptureSettings` stays editable.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field, fields, replace
from enum import StrEnum

from .checks import Severity


class CaptureType(StrEnum):
    CABINET = "cabinet"
    REVERB = "reverb"
    EQ = "eq"
    CUSTOM = "custom"


class Method(StrEnum):
    SWEEP = "sweep"
    DIRAC = "dirac"


class StereoMode(StrEnum):
    MONO = "mono"
    STEREO = "stereo"  # mono in -> stereo out (2-channel IR)
    TRUE_STEREO = "true_stereo"  # L only, then R only (LL, LR, RL, RR)


class TrueStereoOutput(StrEnum):
    TWO_FILES = "two_files"  # <name>_ir_inL.wav + <name>_ir_inR.wav (default: IRene loads stereo files)
    FOUR_CHANNEL = "four_channel"  # one 4-channel WAV: LL, LR, RL, RR


class Normalization(StrEnum):
    OFF = "off"
    PEAK = "peak"
    ENERGY = "energy"


class BitDepth(StrEnum):
    FLOAT32 = "float32"
    PCM24 = "pcm24"


SAMPLE_RATES = (44100, 48000, 88200, 96000, 192000)
DEFAULT_SAMPLE_RATE = 48000

SWEEP_LENGTH_RANGE = (1.0, 30.0)
TEST_LEVEL_RANGE = (-24.0, 0.0)
PEAK_TARGET_RANGE = (-30.0, 0.0)

# IRene (https://github.com/anschae/irene) loads mono and stereo WAV/AIFF files of at most 10 s.
IRENE_MAX_LENGTH_S = 10.0


@dataclass(frozen=True)
class Preset:
    capture_type: CaptureType
    label: str
    stereo_modes: tuple[StereoMode, ...]
    default_stereo_mode: StereoMode
    default_ir_length_s: float
    plausible_range_s: tuple[float, float]
    trim_latency: bool
    normalization: Normalization
    short_hint: str  # sentence: why a short IR is a problem for this type
    long_hint: str  # why a long IR is a problem for this type
    typical: str  # typical range, shown in messages
    noun: str = "this capture"  # with article, for messages
    tail_trim: bool = True  # automatic tail trim on by default
    seconds: bool = False  # show lengths in seconds rather than ms


PRESETS: dict[CaptureType, Preset] = {
    CaptureType.CABINET: Preset(
        CaptureType.CABINET,
        "Cabinet",
        (StereoMode.MONO,),
        StereoMode.MONO,
        0.200,
        (0.020, 1.0),
        trim_latency=True,
        normalization=Normalization.ENERGY,
        short_hint="The low-frequency resonances of the cabinet will be cut off.",
        long_hint="The extra length only adds noise and CPU load in the IR loader.",
        typical="typical cabinet IRs are 50–500 ms",
        noun="a cabinet",
        tail_trim=False,  # cab IRs of a fixed length are easier to compare and blend
    ),
    CaptureType.REVERB: Preset(
        CaptureType.REVERB,
        "Reverb",
        (StereoMode.STEREO, StereoMode.TRUE_STEREO, StereoMode.MONO),
        StereoMode.STEREO,
        4.0,
        (0.5, 20.0),
        trim_latency=False,
        normalization=Normalization.ENERGY,
        short_hint="The tail will be cut off.",
        long_hint="The test file gets very long and the end of the IR will mostly be noise.",
        typical="typical reverbs need 1–10 s",
        noun="a reverb",
        seconds=True,
    ),
    CaptureType.EQ: Preset(
        CaptureType.EQ,
        "EQ / Filter",
        (StereoMode.MONO,),
        StereoMode.MONO,
        0.250,
        (0.010, 2.0),
        trim_latency=False,
        normalization=Normalization.OFF,
        short_hint="Narrow or low-frequency bands ring longer than that and will be cut off.",
        long_hint="The extra length only adds noise and CPU load in the IR loader.",
        typical="typical EQ/filter IRs are 20–500 ms",
        noun="an EQ or filter",
    ),
    CaptureType.CUSTOM: Preset(
        CaptureType.CUSTOM,
        "Custom",
        (StereoMode.MONO, StereoMode.STEREO, StereoMode.TRUE_STEREO),
        StereoMode.MONO,
        1.0,
        (0.001, 30.0),
        trim_latency=False,
        normalization=Normalization.OFF,
        short_hint="The response will be cut off.",
        long_hint="The test file gets very long.",
        typical="choose a length that covers the whole decay",
    ),
}


@dataclass
class CaptureSettings:
    """Everything the user can set. Stored in the session file."""

    capture_type: CaptureType = CaptureType.CABINET
    method: Method = Method.SWEEP
    sample_rate: int = DEFAULT_SAMPLE_RATE
    ir_length_s: float = 0.200
    stereo_mode: StereoMode = StereoMode.MONO
    true_stereo_output: TrueStereoOutput = TrueStereoOutput.TWO_FILES
    # Advanced
    sweep_length_s: float = 6.0
    f_start_hz: float = 20.0
    f_end_hz: float | None = None  # None = 0.95 x Nyquist
    test_level_dbfs: float = -6.0
    test_bit_depth: BitDepth = BitDepth.FLOAT32
    ir_bit_depth: BitDepth = BitDepth.PCM24
    trim_latency: bool = True
    tail_trim: bool = True
    normalization: Normalization = Normalization.ENERGY
    peak_target_dbfs: float = -1.0
    capture_name: str = ""
    extra: dict = field(default_factory=dict)

    @classmethod
    def for_type(cls, capture_type: CaptureType | str, **overrides) -> CaptureSettings:
        """Settings with the preset defaults of ``capture_type``, then ``overrides``."""
        preset = PRESETS[CaptureType(capture_type)]
        s = cls(
            capture_type=preset.capture_type,
            ir_length_s=preset.default_ir_length_s,
            stereo_mode=preset.default_stereo_mode,
            trim_latency=preset.trim_latency,
            normalization=preset.normalization,
            tail_trim=preset.tail_trim,
        )
        return replace(s, **overrides) if overrides else s

    @property
    def effective_f_end_hz(self) -> float:
        return float(self.f_end_hz) if self.f_end_hz else 0.95 * self.sample_rate / 2

    @property
    def input_channels(self) -> int:
        """Channels of the test file."""
        return 1 if self.stereo_mode == StereoMode.MONO else 2

    def to_dict(self) -> dict:
        d = asdict(self)
        for k, v in d.items():
            if isinstance(v, StrEnum):
                d[k] = v.value
        return d

    @classmethod
    def from_dict(cls, d: dict) -> CaptureSettings:
        known = {f.name for f in fields(cls)}
        kwargs = {k: v for k, v in d.items() if k in known}
        enums = {
            "capture_type": CaptureType,
            "method": Method,
            "stereo_mode": StereoMode,
            "true_stereo_output": TrueStereoOutput,
            "test_bit_depth": BitDepth,
            "ir_bit_depth": BitDepth,
            "normalization": Normalization,
        }
        for k, enum in enums.items():
            if k in kwargs and kwargs[k] is not None:
                kwargs[k] = enum(kwargs[k])
        if "sample_rate" in kwargs:
            kwargs["sample_rate"] = int(kwargs["sample_rate"])
        return cls(**kwargs)


@dataclass(frozen=True)
class ValidationMessage:
    field: str
    severity: Severity
    text: str


def format_seconds(s: float, seconds: bool = False) -> str:
    if s < 1.0 and not seconds:
        return f"{s * 1000:g} ms"
    return f"{s:g} s"


def validate(settings: CaptureSettings) -> list[ValidationMessage]:
    """Plain-language messages about values that are unusual or impossible.

    Warnings let the user continue; errors mean the value can't be used.
    """
    out: list[ValidationMessage] = []
    preset = PRESETS[settings.capture_type]
    nyq = settings.sample_rate / 2
    lo, hi = preset.plausible_range_s
    length = settings.ir_length_s
    kind = preset.noun

    if settings.sample_rate not in SAMPLE_RATES:
        out.append(ValidationMessage("sample_rate", Severity.ERROR,
                                     f"{settings.sample_rate} Hz is not supported. Use one of "
                                     + ", ".join(f"{r / 1000:g} kHz" for r in SAMPLE_RATES) + "."))
    if length <= 0:
        out.append(ValidationMessage("ir_length_s", Severity.ERROR, "The IR length must be longer than 0."))
    elif length < lo:
        out.append(ValidationMessage(
            "ir_length_s", Severity.WARNING,
            f"{format_seconds(length, preset.seconds)} is very short for {kind}; {preset.typical}. "
            f"{preset.short_hint}"))
    elif length > hi:
        out.append(ValidationMessage(
            "ir_length_s", Severity.WARNING,
            f"{format_seconds(length, preset.seconds)} is very long for {kind}; {preset.typical}. "
            f"{preset.long_hint}"))
    if length > IRENE_MAX_LENGTH_S:
        out.append(ValidationMessage(
            "ir_length_s", Severity.INFO,
            f"IRene loads at most {IRENE_MAX_LENGTH_S:g} s of an IR; longer IRs are cut there."))

    if settings.stereo_mode not in preset.stereo_modes:
        allowed = ", ".join(m.value.replace("_", " ") for m in preset.stereo_modes)
        out.append(ValidationMessage("stereo_mode", Severity.ERROR,
                                     f"{preset.label} captures support: {allowed}."))

    if settings.method == Method.SWEEP:
        a, b = SWEEP_LENGTH_RANGE
        if not a <= settings.sweep_length_s <= b:
            out.append(ValidationMessage("sweep_length_s", Severity.ERROR,
                                         f"The sweep length must be between {a:g} and {b:g} s."))
    f0, f1 = settings.f_start_hz, settings.effective_f_end_hz
    if f0 < 1.0:
        out.append(ValidationMessage("f_start_hz", Severity.ERROR, "The start frequency must be at least 1 Hz."))
    if f1 > 0.99 * nyq:
        out.append(ValidationMessage("f_end_hz", Severity.ERROR,
                                     f"The end frequency must stay below {0.99 * nyq:.0f} Hz (99 % of Nyquist)."))
    if f0 * 2 > f1:
        out.append(ValidationMessage("f_start_hz", Severity.ERROR,
                                     "The frequency range must span at least one octave."))
    elif f0 > 50:
        out.append(ValidationMessage("f_start_hz", Severity.WARNING,
                                     f"Starting at {f0:g} Hz leaves out the lowest frequencies; the IR will "
                                     "have no reliable bass below that."))
    if f1 < 0.8 * nyq and f1 < 18000:
        out.append(ValidationMessage("f_end_hz", Severity.WARNING,
                                     f"Ending at {f1:g} Hz leaves out the highest frequencies; the IR will "
                                     "have no reliable treble above that."))

    a, b = TEST_LEVEL_RANGE
    if not a <= settings.test_level_dbfs <= b:
        out.append(ValidationMessage("test_level_dbfs", Severity.ERROR,
                                     f"The test level must be between {a:g} and {b:g} dBFS."))
    if settings.test_bit_depth == BitDepth.PCM24:
        out.append(ValidationMessage("test_bit_depth", Severity.INFO,
                                     "A 32-bit float test file is better: it is exact and can't clip."))
    if settings.normalization == Normalization.PEAK:
        a, b = PEAK_TARGET_RANGE
        if not a <= settings.peak_target_dbfs <= b:
            out.append(ValidationMessage("peak_target_dbfs", Severity.ERROR,
                                         f"The peak target must be between {a:g} and {b:g} dBFS."))
    return out
