"""WAV/AIFF/FLAC reading and writing, format detection, conversions.

Everything is converted to float64 (frames, channels) internally. Integer files are
written from pre-scaled int32 data so that writing and reading back is exact.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import soundfile as sf

from .presets import BitDepth

READABLE_EXTENSIONS = (".wav", ".wave", ".aif", ".aiff", ".flac")


@dataclass
class RenderedAudio:
    """Audio coming back from a backend (DAW file, plugin host, later hardware)."""

    data: np.ndarray  # float64 (frames, channels)
    sample_rate: int
    subtype: str = "DOUBLE"  # soundfile subtype: PCM_16, PCM_24, PCM_32, FLOAT, DOUBLE
    path: Path | None = None
    name: str = ""
    extra: dict = field(default_factory=dict)

    @property
    def is_float(self) -> bool:
        return self.subtype.upper() in ("FLOAT", "DOUBLE")

    @property
    def bits(self) -> int | None:
        s = self.subtype.upper()
        if s.startswith("PCM_"):
            try:
                return int(s[4:])
            except ValueError:
                return None
        return 32 if s == "FLOAT" else 64 if s == "DOUBLE" else None

    @property
    def channels(self) -> int:
        return int(self.data.shape[1])

    @property
    def frames(self) -> int:
        return int(self.data.shape[0])


def read_audio(path: str | Path) -> RenderedAudio:
    path = Path(path)
    try:
        info = sf.info(str(path))
        data, rate = sf.read(str(path), dtype="float64", always_2d=True)
    except (RuntimeError, sf.LibsndfileError) as e:
        raise ValueError(f"Can't read {path.name}: {e}. Use WAV, AIFF or FLAC.") from e
    return RenderedAudio(data=data, sample_rate=int(rate), subtype=info.subtype, path=path, name=path.stem)


def write_audio(path: str | Path, data: np.ndarray, sample_rate: int, bit_depth: BitDepth,
                comment: str = "", software: str = "") -> None:
    """Write (frames, channels) float data. 24-bit values are rounded, not dithered."""
    path = Path(path)
    data = np.atleast_2d(np.asarray(data, dtype=np.float64))
    if data.shape[0] < data.shape[1]:
        data = data.T
    if bit_depth == BitDepth.PCM24:
        q = np.clip(np.round(data * 2**23), -(2**23), 2**23 - 1).astype(np.int32) << 8
        subtype, payload = "PCM_24", q
    else:
        subtype, payload = "FLOAT", data.astype(np.float32)
    fmt = "AIFF" if path.suffix.lower() in (".aif", ".aiff") else "WAV"
    with sf.SoundFile(str(path), "w", samplerate=int(sample_rate), channels=data.shape[1],
                      subtype=subtype, format=fmt) as f:
        if comment:
            f.comment = comment[:250]
        if software:
            f.software = software[:250]
        f.write(payload)


def unique_path(path: Path, siblings: tuple[str, ...] = ()) -> Path:
    """``path`` if neither it nor its sibling suffixes exist, else ``name_2``, ``name_3``, …

    ``siblings`` are replacement suffixes checked too (e.g. ``.json`` next to a ``.wav``).
    """
    def taken(p: Path) -> bool:
        return p.exists() or any(p.with_suffix(s).exists() for s in siblings)

    if not taken(path):
        return path
    i = 2
    while True:
        cand = path.with_name(f"{path.stem}_{i}{path.suffix}")
        if not taken(cand):
            return cand
        i += 1
