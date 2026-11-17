"""Platform paths (no Qt dependency): config folder and bundled resources."""

from __future__ import annotations

import os
import sys
from pathlib import Path


def config_dir() -> Path:
    if override := os.environ.get("IRENE_CAPTURE_CONFIG_DIR"):
        p = Path(override)
    elif sys.platform == "win32":
        p = Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming")) / "irene-capture"
    elif sys.platform == "darwin":
        p = Path.home() / "Library" / "Application Support" / "irene-capture"
    else:
        p = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "irene-capture"
    p.mkdir(parents=True, exist_ok=True)
    return p


def resource_path(name: str) -> Path:
    """A file from ``irene_capture/resources`` (works from source, wheel and PyInstaller)."""
    base = getattr(sys, "_MEIPASS", None)
    if base:
        p = Path(base) / "irene_capture" / "resources" / name
        if p.exists():
            return p
    return Path(__file__).resolve().parent / "resources" / name
