"""DAW round trip: write the test file, the user renders it, import the result.

Also: the DAW instructions (from ``resources/instructions.md``) and finding the session
that belongs to a rendered file.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

from ..core.audio_io import RenderedAudio, read_audio, write_audio
from ..core.presets import CaptureType, Normalization, StereoMode
from ..core.session import SESSION_SUFFIX, Session, load_session, session_path_for
from ..core.signals import render_test_signal
from ..paths import config_dir, resource_path
from .base import CaptureBackend

DAWS = ("Cubase", "Logic Pro")
GENERAL = "General tips"


# --- instructions ---------------------------------------------------------------------------


def _conditions(session: Session) -> set[str]:
    s = session.settings
    c = {s.capture_type.value}
    c.add("mono" if s.stereo_mode == StereoMode.MONO else "stereo")
    if s.stereo_mode == StereoMode.TRUE_STEREO:
        c.add("truestereo")
    if s.normalization == Normalization.OFF:
        c.add("absolute")
    return c


def parse_sections(text: str) -> dict[str, list[str]]:
    text = re.sub(r"<!--.*?-->", "", text, flags=re.S)
    sections: dict[str, list[str]] = {}
    current = None
    for line in text.splitlines():
        if line.startswith("# "):
            current = line[2:].strip()
            sections[current] = []
        elif current is not None:
            sections[current].append(line)
    return sections


def render_instructions(session: Session, test_file: str | None = None, text: str | None = None) -> dict[str, str]:
    """Markdown per DAW, with the general tips appended and the session's values filled in."""
    if text is None:
        text = resource_path("instructions.md").read_text(encoding="utf-8")
    s = session.settings
    test_file = test_file or session.test_file
    values = {
        "rate": f"{s.sample_rate / 1000:g} kHz",
        "rate_hz": str(s.sample_rate),
        "track": "mono" if s.stereo_mode == StereoMode.MONO else "stereo",
        "mono_downmix": "on" if s.stereo_mode == StereoMode.MONO else "off",
        "test_file": test_file,
        "session_file": Path(test_file).name + SESSION_SUFFIX,
        "length": f"{session.layout.total_length / s.sample_rate:.1f} s",
    }
    conds = _conditions(session)

    def render(lines: list[str]) -> str:
        out = []
        for line in lines:
            m = re.match(r"^\?([\w|]+) ?(.*)$", line)
            if m:
                if not set(m.group(1).split("|")) & conds:
                    continue
                line = m.group(2)
            out.append(re.sub(r"\{(\w+)\}", lambda mm: values.get(mm.group(1), mm.group(0)), line))
        return "\n".join(out).strip()

    sections = parse_sections(text)
    general = render(sections.get(GENERAL, []))
    result = {}
    for daw in DAWS:
        body = render(sections.get(daw, []))
        result[daw] = f"{body}\n\n**Tips**\n\n{general}\n"
    return result


def markdown_to_text(md: str) -> str:
    md = md.replace("**", "").replace("`", "")
    return re.sub(r"^\*(.+)\*", r"\1", md, flags=re.M)


def instructions_text(session: Session, test_file: str | None = None) -> str:
    parts = [f"irene-capture — how to render {test_file or session.test_file}", ""]
    for daw, md in render_instructions(session, test_file).items():
        parts += [daw, "=" * len(daw), "", markdown_to_text(md), ""]
    return "\n".join(parts)


# --- session index -----------------------------------------------------------------------


@dataclass
class IndexEntry:
    path: str
    short_id: str
    created: str


class SessionIndex:
    """Recently created sessions (a small JSON file in the config folder)."""

    MAX = 200

    def __init__(self, path: Path | None = None):
        self.path = path or (config_dir() / "sessions.json")

    def entries(self) -> list[IndexEntry]:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            return [IndexEntry(**e) for e in data]
        except (OSError, ValueError, TypeError):
            return []

    def add(self, session: Session) -> None:
        if session.path is None:
            return
        entries = [e for e in self.entries() if e.path != str(session.path)]
        entries.insert(0, IndexEntry(str(session.path), session.short_id, session.created))
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(json.dumps([e.__dict__ for e in entries[:self.MAX]], indent=1), encoding="utf-8")
        except OSError:
            pass


def short_ids_in(name: str) -> list[str]:
    return [m.lower() for m in re.findall(r"(?<![0-9a-fA-F])([0-9a-fA-F]{4})(?![0-9a-fA-F])", name)]


def find_session(rendered: Path, index: SessionIndex | None = None,
                 extra_dirs: tuple[Path, ...] = ()) -> tuple[Session | None, str]:
    """Find the session for a rendered file.

    1. by the short ID in the file name (JSON files next to the file, in ``extra_dirs`` and in the index);
    2. otherwise the most recent session from the index;
    3. otherwise None (ask the user).

    Returns (session, how) where ``how`` explains the choice in plain language.
    """
    index = index or SessionIndex()
    rendered = Path(rendered)
    ids = short_ids_in(rendered.stem)
    candidates: list[Path] = []
    for d in (rendered.parent, rendered.parent.parent, *extra_dirs):
        try:
            candidates += sorted(Path(d).glob("*" + SESSION_SUFFIX))
        except OSError:
            pass
    entries = index.entries()
    candidates += [Path(e.path) for e in entries]
    for sid in ids:
        for c in candidates:
            if c.name.endswith(f"_{sid}.wav{SESSION_SUFFIX}") or f"_{sid}." in c.name or f"_{sid}_" in c.name:
                try:
                    s = load_session(c)
                except (ValueError, OSError):
                    continue
                if s.short_id == sid:
                    return s, f"Session {sid} found by the ID in the file name ({c.name})."
    for e in entries:
        p = Path(e.path)
        if p.exists():
            try:
                s = load_session(p)
            except (ValueError, OSError):
                continue
            return s, (f"No session ID in the file name, so the most recent session ({s.short_id}, "
                       f"{p.name}) is used. If that's wrong, choose the .capture.json file.")
    return None, "No session found. Choose the .capture.json file that belongs to the test file."


# --- backend ----------------------------------------------------------------------------


class DawRoundtripBackend(CaptureBackend):
    source_kind = "file"
    name = "Render in DAW"

    def __init__(self, index: SessionIndex | None = None):
        self.index = index

    def create_test_file(self, session: Session, path: str | Path) -> list[Path]:
        """Write the test file, its session JSON and the instructions text. Returns the paths."""
        path = Path(path)
        if path.suffix.lower() not in (".wav", ".wave"):
            path = path.with_suffix(".wav")
        path.parent.mkdir(parents=True, exist_ok=True)
        session.test_file = path.name
        s = session.settings
        write_audio(path, render_test_signal(session), s.sample_rate, s.test_bit_depth,
                    comment=f"irene-capture test file, session {session.short_id}", software="irene-capture")
        js = session.save(session_path_for(path))
        txt = path.with_name(path.stem + "_instructions.txt")
        txt.write_text(instructions_text(session, path.name), encoding="utf-8")
        if self.index is not None:
            self.index.add(session)
        return [path, js, txt]

    def capture(self, session: Session, path: str | Path | None = None, **kwargs) -> RenderedAudio:
        if path is None:
            raise ValueError("The DAW route needs the rendered file.")
        return read_audio(path)


def is_capture_type(value: str) -> bool:
    return value in {t.value for t in CaptureType}
