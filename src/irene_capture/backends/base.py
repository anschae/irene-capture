"""Capture backends: where the rendered audio comes from.

Every backend turns a :class:`~irene_capture.core.session.Session` into a
:class:`~irene_capture.core.audio_io.RenderedAudio`. The analysis is the same for all of
them (:func:`irene_capture.core.analyze`).

- :class:`~.daw.DawRoundtripBackend`: writes a test file, the user renders it in a DAW.
- :class:`~.plugin_host.PluginHostBackend`: renders through a VST3/AU plugin directly.
- :class:`~.hardware.HardwareBackend`: audio interface (v2, not implemented).
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from ..core.audio_io import RenderedAudio
from ..core.checks import CheckResult
from ..core.session import Session


class CaptureBackend(ABC):
    #: "file" (DAW renders: bit-depth/clipping checks apply) or "plugin"/"hardware".
    source_kind: str = "file"
    #: Shown in the GUI; False hides the backend.
    available: bool = True
    name: str = ""

    @abstractmethod
    def capture(self, session: Session, **kwargs) -> RenderedAudio:
        """Produce the rendered audio for ``session``."""

    def extra_checks(self) -> list[CheckResult]:
        """Checks only this backend can make (e.g. determinism of a plugin)."""
        return []
