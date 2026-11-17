"""Session model and its JSON file.

A session describes one test file: all parameters plus the exact layout in samples.
Signals are generated deterministically, so the dry test signal can always be
regenerated from the JSON. DAWs strip WAV metadata, which is why the JSON lives next
to the test file as ``<testfile>.capture.json``.

Layout of one pass (all offsets in samples, relative to the start of the pass)::

    | pre-roll | capture signal | capture tail | verify signal | verify tail | post-roll |
    0          capture_start    capture_end    verify_start    verify_end    verify_tail_end  pass_length

True stereo uses two passes: the whole sequence on L (R silent), then on R (L silent).
"""

from __future__ import annotations

import json
import secrets
import uuid
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from .._version import __version__
from .presets import CaptureSettings, CaptureType, Method, StereoMode

SCHEMA_VERSION = 1
SESSION_SUFFIX = ".capture.json"

# Fixed parts of the signal design (stored in the session so they could change later).
PRE_ROLL_S = 0.5
TAIL_MARGIN_S = 0.5
VERIFY_LENGTH_S = 4.0
VERIFY_RMS_DBFS = -20.0
VERIFY_FADE_S = 0.020
SWEEP_FADE_IN_S = 0.050
SWEEP_FADE_OUT_S = 0.010
POST_ROLL_S = 0.5
NOISE_SEED = 20240611


@dataclass(frozen=True)
class SignalDesign:
    pre_roll_s: float = PRE_ROLL_S
    tail_margin_s: float = TAIL_MARGIN_S
    verify_length_s: float = VERIFY_LENGTH_S
    verify_rms_dbfs: float = VERIFY_RMS_DBFS
    verify_fade_s: float = VERIFY_FADE_S
    sweep_fade_in_s: float = SWEEP_FADE_IN_S
    sweep_fade_out_s: float = SWEEP_FADE_OUT_S
    post_roll_s: float = POST_ROLL_S
    noise_seed: int = NOISE_SEED


@dataclass(frozen=True)
class Pass:
    """One run of the sequence. ``input_channel`` None = all test-file channels carry the signal."""

    index: int
    offset: int
    input_channel: int | None


@dataclass(frozen=True)
class Layout:
    sample_rate: int
    channels: int
    pre_roll: int
    capture_start: int
    capture_length: int
    capture_end: int
    capture_tail_end: int
    verify_start: int
    verify_end: int
    verify_tail_end: int
    pass_length: int
    passes: tuple[Pass, ...]

    @property
    def total_length(self) -> int:
        return self.pass_length * len(self.passes)

    @property
    def ir_length(self) -> int:
        """Samples available after the zero-delay point in the capture tail (IR length + margin)."""
        return self.capture_tail_end - self.capture_end

    def to_dict(self) -> dict:
        d = asdict(self)
        d["total_length"] = self.total_length
        return d

    @classmethod
    def from_dict(cls, d: dict) -> Layout:
        d = dict(d)
        d.pop("total_length", None)
        d["passes"] = tuple(Pass(**p) for p in d["passes"])
        return cls(**d)


@dataclass(frozen=True)
class HardwareSetup:
    """Fields for the hardware route (v2). Not used in v1, but part of the schema."""

    repetitions: int = 1  # capture signal repeated and averaged
    loopback_channel: int | None = None  # input channel with a loopback reference for latency
    input_device: str | None = None
    output_device: str | None = None
    input_gain_db: float | None = None
    output_gain_db: float | None = None
    level_calibration_dbfs: float | None = None  # level at the device input for 0 dBFS out


@dataclass
class Session:
    session_id: str
    short_id: str
    created: str
    tool_version: str
    settings: CaptureSettings
    design: SignalDesign
    layout: Layout
    hardware: HardwareSetup = field(default_factory=HardwareSetup)
    test_file: str = ""
    source: str = "daw"  # daw | plugin | hardware
    path: Path | None = None  # where the JSON was loaded from / saved to (not serialized)

    # --- convenience -------------------------------------------------------------------
    @property
    def sample_rate(self) -> int:
        return self.settings.sample_rate

    @property
    def method(self) -> Method:
        return self.settings.method

    @property
    def stereo_mode(self) -> StereoMode:
        return self.settings.stereo_mode

    @property
    def output_channels_expected(self) -> int:
        return 1 if self.stereo_mode == StereoMode.MONO else 2

    @property
    def n_inputs(self) -> int:
        return 2 if self.stereo_mode == StereoMode.TRUE_STEREO else 1

    def default_test_filename(self) -> str:
        s = self.settings
        rate = f"{s.sample_rate / 1000:g}k".replace(".", "_")
        mode = {"mono": "", "stereo": "_st", "true_stereo": "_ts"}[s.stereo_mode.value]
        return f"irc_{s.capture_type.value}_{s.method.value}_{rate}{mode}_{self.short_id}.wav"

    def to_dict(self) -> dict:
        return {
            "schema_version": SCHEMA_VERSION,
            "tool": "irene-capture",
            "tool_version": self.tool_version,
            "session_id": self.session_id,
            "short_id": self.short_id,
            "created": self.created,
            "source": self.source,
            "test_file": self.test_file,
            "settings": self.settings.to_dict(),
            "design": asdict(self.design),
            "layout": self.layout.to_dict(),
            "hardware": asdict(self.hardware),
        }

    @classmethod
    def from_dict(cls, d: dict) -> Session:
        version = int(d.get("schema_version", 0))
        if version > SCHEMA_VERSION:
            raise ValueError(
                f"This session file was written by a newer irene-capture (schema {version}). Please update the app.")
        settings = CaptureSettings.from_dict(d["settings"])
        design = SignalDesign(**d.get("design", {}))
        session = cls(
            session_id=d["session_id"],
            short_id=d["short_id"],
            created=d.get("created", ""),
            tool_version=d.get("tool_version", "?"),
            settings=settings,
            design=design,
            layout=Layout.from_dict(d["layout"]),
            hardware=HardwareSetup(**d.get("hardware", {})),
            test_file=d.get("test_file", ""),
            source=d.get("source", "daw"),
        )
        # A session file must match what this version would generate, or the regenerated
        # dry signal would not match the test file.
        expected = compute_layout(settings, design)
        if expected != session.layout:
            raise ValueError("The session file's layout does not match its settings (file edited or damaged?).")
        return session

    def save(self, path: str | Path) -> Path:
        path = Path(path)
        path.write_text(json.dumps(self.to_dict(), indent=2), encoding="utf-8")
        self.path = path
        return path


def compute_layout(settings: CaptureSettings, design: SignalDesign | None = None) -> Layout:
    design = design or SignalDesign()
    sr = int(settings.sample_rate)

    def n(seconds: float) -> int:
        return int(round(seconds * sr))

    pre = n(design.pre_roll_s)
    cap_len = n(settings.sweep_length_s) if settings.method == Method.SWEEP else 1
    tail = n(settings.ir_length_s + design.tail_margin_s)
    cap_start = pre
    cap_end = cap_start + cap_len
    cap_tail_end = cap_end + tail
    ver_start = cap_tail_end
    ver_end = ver_start + n(design.verify_length_s)
    ver_tail_end = ver_end + tail
    pass_len = ver_tail_end + n(design.post_roll_s)
    if settings.stereo_mode == StereoMode.TRUE_STEREO:
        passes = (Pass(0, 0, 0), Pass(1, pass_len, 1))
    else:
        passes = (Pass(0, 0, None),)
    return Layout(sr, settings.input_channels, pre, cap_start, cap_len, cap_end, cap_tail_end,
                  ver_start, ver_end, ver_tail_end, pass_len, passes)


def create_session(settings: CaptureSettings, source: str = "daw") -> Session:
    """New session with a fresh ID. Raises ValueError for settings that can't work."""
    from .presets import validate

    errors = [m for m in validate(settings) if m.severity == "error"]
    if errors:
        raise ValueError(" ".join(m.text for m in errors))
    if settings.capture_type not in CaptureType:
        raise ValueError(f"Unknown capture type {settings.capture_type}")
    design = SignalDesign()
    sid = uuid.uuid4().hex
    session = Session(
        session_id=sid,
        short_id=secrets.token_hex(2),
        created=datetime.now(UTC).isoformat(timespec="seconds"),
        tool_version=__version__,
        settings=settings,
        design=design,
        layout=compute_layout(settings, design),
        source=source,
    )
    session.test_file = session.default_test_filename()
    return session


def load_session(path: str | Path) -> Session:
    path = Path(path)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        raise ValueError(f"Can't read session file {path.name}: {e}") from e
    session = Session.from_dict(data)
    session.path = path
    return session


def session_path_for(test_file: str | Path) -> Path:
    test_file = Path(test_file)
    return test_file.with_name(test_file.name + SESSION_SUFFIX)
