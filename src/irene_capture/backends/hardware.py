"""Hardware capture via an audio interface — planned for v2, not implemented.

See docs/ARCHITECTURE.md ("Adding the hardware backend") for what it will need:
audio I/O (e.g. ``sounddevice``), latency calibration with a loopback channel, repetitions
and averaging (``Session.hardware.repetitions``), longer sweeps for SNR and level
calibration. The session schema already has the fields (:class:`HardwareSetup`).
"""

from __future__ import annotations

from ..core.audio_io import RenderedAudio
from ..core.session import Session
from .base import CaptureBackend


class HardwareBackend(CaptureBackend):
    source_kind = "hardware"
    available = False  # hidden in the GUI
    name = "Hardware (audio interface)"

    def capture(self, session: Session, **kwargs) -> RenderedAudio:
        raise NotImplementedError("Hardware capture is planned for version 2.")
