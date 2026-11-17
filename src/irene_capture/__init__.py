"""irene-capture: capture impulse responses from audio plugins."""

from ._version import __version__

TOOL_NAME = "irene-capture"

__all__ = ["TOOL_NAME", "__version__"]


def build_info() -> str:
    """Version string with the git commit if CI wrote one into ``_build_info.py``."""
    try:
        from ._build_info import COMMIT  # type: ignore[import-not-found]
    except ImportError:
        COMMIT = ""
    return f"{__version__} ({COMMIT})" if COMMIT else __version__
