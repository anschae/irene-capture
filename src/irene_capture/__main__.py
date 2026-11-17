"""Entry point: GUI without arguments, CLI with arguments.

``multiprocessing.freeze_support()`` must run first: the plugin host is a spawned worker
process, and in a PyInstaller build the worker is this same executable.
"""

from __future__ import annotations

import multiprocessing
import sys


def _attach_windows_console() -> None:
    """The Windows .exe is a windowed app; show CLI output in the calling console."""
    if sys.platform != "win32" or not getattr(sys, "frozen", False):
        return
    try:
        import ctypes

        if ctypes.windll.kernel32.AttachConsole(-1):  # ATTACH_PARENT_PROCESS
            sys.stdout = open("CONOUT$", "w", encoding="utf-8", errors="replace")  # noqa: SIM115
            sys.stderr = sys.stdout
    except Exception:
        pass


def _ensure_streams() -> None:
    # In a windowed build without a console these are None; give them a sink.
    import io

    if sys.stdout is None:
        sys.stdout = io.StringIO()
    if sys.stderr is None:
        sys.stderr = io.StringIO()


def main() -> int:
    multiprocessing.freeze_support()
    args = sys.argv[1:]
    if args and not args[0].startswith("-psn"):  # macOS Finder may pass -psn_...
        _attach_windows_console()
        _ensure_streams()
        from .cli import main as cli_main

        return cli_main(args)
    _ensure_streams()
    from .gui.app import run_gui

    return run_gui()


if __name__ == "__main__":
    sys.exit(main())
