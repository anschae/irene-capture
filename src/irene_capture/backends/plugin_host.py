"""Direct plugin route: host a VST3/AU plugin with pedalboard in a separate worker process.

Why a worker process (``multiprocessing`` with ``spawn``):

- a crashing plugin can't take the app down;
- the plugin editor gets its own main thread and event loop (macOS requires UI on the main
  thread, and JUCE's editor loop must not fight Qt's).

The worker never imports Qt. Commands go parent → worker on one pipe, replies come back
on another. ``show_editor`` blocks the worker's main thread; a reader thread in the worker
still receives ``close_editor`` and sets the editor's close event.
"""

from __future__ import annotations

import multiprocessing as mp
import queue
import sys
import threading
import traceback
from pathlib import Path

import numpy as np

from ..core.analysis import AnalysisOptions, analyze
from ..core.audio_io import RenderedAudio
from ..core.checks import CheckResult, Severity, db
from ..core.result import CaptureResult
from ..core.session import Session
from ..core.signals import render_test_signal
from .base import CaptureBackend

WARMUP_S = 1.0
DETERMINISM_LIMIT_DB = -100.0

STANDARD_FOLDERS = {
    "win32": [Path(r"C:\Program Files\Common Files\VST3")],
    "darwin": [Path("/Library/Audio/Plug-Ins/VST3"), Path("~/Library/Audio/Plug-Ins/VST3").expanduser(),
               Path("/Library/Audio/Plug-Ins/Components"), Path("~/Library/Audio/Plug-Ins/Components").expanduser()],
    "linux": [Path("~/.vst3").expanduser(), Path("/usr/lib/vst3"), Path("/usr/local/lib/vst3")],
}
PLUGIN_SUFFIXES = (".vst3", ".component")


class PluginError(Exception):
    """Plain-language error from the plugin host."""


class PluginHostCrashed(PluginError):
    pass


def standard_folders() -> list[Path]:
    key = "win32" if sys.platform == "win32" else "darwin" if sys.platform == "darwin" else "linux"
    return STANDARD_FOLDERS[key]


def scan_plugins(extra_folders: list[Path] | None = None, max_depth: int = 4) -> list[Path]:
    """Plugin bundles/files in the standard folders plus ``extra_folders`` (no loading)."""
    found: list[Path] = []
    seen: set[str] = set()

    def walk(d: Path, depth: int) -> None:
        try:
            entries = sorted(d.iterdir(), key=lambda p: p.name.lower())
        except OSError:
            return
        for p in entries:
            if p.suffix.lower() in PLUGIN_SUFFIXES and not (sys.platform != "darwin" and p.suffix == ".component"):
                key = str(p.resolve()).lower()
                if key not in seen:
                    seen.add(key)
                    found.append(p)
            elif p.is_dir() and depth < max_depth:
                walk(p, depth + 1)

    for folder in [*standard_folders(), *(extra_folders or [])]:
        if folder.exists():
            walk(folder, 0)
    return found


# --- worker side ---------------------------------------------------------------------------


def _make_builtin(spec: str):
    import pedalboard

    _, _, rest = spec.partition(":")
    name, _, params = rest.partition(":")
    cls = getattr(pedalboard, name, None)
    if cls is None or not isinstance(cls, type):
        raise PluginError(f"Unknown built-in effect {name!r}.")
    kwargs = {}
    for item in filter(None, params.split(",")):
        k, _, v = item.partition("=")
        kwargs[k.strip()] = float(v)
    return cls(**kwargs)


def _plugin_info(plugin, path: str) -> dict:
    name = getattr(plugin, "name", None) or type(plugin).__name__
    return {
        "name": str(name),
        "path": path,
        "manufacturer": str(getattr(plugin, "manufacturer_name", "") or ""),
        "version": str(getattr(plugin, "version", "") or ""),
        "latency": int(getattr(plugin, "reported_latency_samples", 0) or 0),
        "has_editor": hasattr(plugin, "show_editor"),
        "is_external": hasattr(plugin, "raw_state"),
        "format": "VST3" if path.lower().endswith(".vst3") else "AU" if path.lower().endswith(".component")
        else "built-in",
    }


def _params(plugin) -> list[dict]:
    out = []
    for key, p in getattr(plugin, "parameters", {}).items():
        try:
            out.append({
                "key": key, "name": getattr(p, "name", key), "raw_value": float(p.raw_value),
                "text": str(getattr(p, "string_value", "")), "label": str(getattr(p, "label", "") or ""),
                "num_steps": int(getattr(p, "num_steps", 0) or 0),
                "valid_values": [str(v) for v in (getattr(p, "valid_values", None) or [])][:64],
            })
        except Exception:  # some parameters can't be read; skip them
            continue
    return out


def _process(plugin, audio: np.ndarray, sr: float, warmup: int) -> np.ndarray:
    """(frames, ch) → (frames, ch_out). Warm-up silence first, then the signal; reset before."""
    x = np.concatenate([np.zeros((warmup, audio.shape[1]), np.float32), audio.astype(np.float32)]).T
    try:
        y = plugin.process(np.ascontiguousarray(x), sr, reset=True)
    except Exception:
        if x.shape[0] == 1:  # plugin may not support mono: feed L = R
            y = plugin.process(np.ascontiguousarray(np.repeat(x, 2, axis=0)), sr, reset=True)
        else:
            raise
    y = np.asarray(y, dtype=np.float64).T
    y = y[warmup:]
    if y.shape[0] < audio.shape[0]:
        y = np.concatenate([y, np.zeros((audio.shape[0] - y.shape[0], y.shape[1]))])
    return y[:audio.shape[0]]


def worker_main(cmd_conn, res_conn) -> None:  # pragma: no cover - runs in the child process
    import pedalboard

    inbox: queue.Queue = queue.Queue()
    editor_event: list[threading.Event | None] = [None]

    def reader():
        while True:
            try:
                msg = cmd_conn.recv()
            except (EOFError, OSError):
                inbox.put({"cmd": "quit"})
                return
            if msg.get("cmd") == "close_editor":
                ev = editor_event[0]
                if ev is not None:
                    ev.set()
                continue
            inbox.put(msg)

    threading.Thread(target=reader, daemon=True).start()
    plugin = None
    path = ""
    while True:
        msg = inbox.get()
        cmd = msg.get("cmd")
        try:
            if cmd == "quit":
                res_conn.send({"ok": True})
                return
            if cmd == "names":
                names = list(pedalboard.VST3Plugin.get_plugin_names_for_file(msg["path"])) \
                    if msg["path"].lower().endswith(".vst3") else \
                    list(pedalboard.AudioUnitPlugin.get_plugin_names_for_file(msg["path"]))
                res_conn.send({"ok": True, "names": names})
            elif cmd == "load":
                path = msg["path"]
                if path.startswith("builtin:"):
                    plugin = _make_builtin(path)
                else:
                    kwargs = {"plugin_name": msg["name"]} if msg.get("name") else {}
                    plugin = pedalboard.load_plugin(path, **kwargs)
                    if getattr(plugin, "is_instrument", False) and not getattr(plugin, "is_effect", True):
                        raise PluginError("This is an instrument, not an effect.")
                res_conn.send({"ok": True, "info": _plugin_info(plugin, path)})
            elif plugin is None:
                res_conn.send({"ok": False, "error": "No plugin loaded."})
            elif cmd == "show_editor":
                if not hasattr(plugin, "show_editor"):
                    res_conn.send({"ok": False, "error": "This plugin has no editor."})
                    continue
                ev = threading.Event()
                editor_event[0] = ev
                if msg.get("auto_close_s"):
                    threading.Timer(float(msg["auto_close_s"]), ev.set).start()
                plugin.show_editor(ev)
                editor_event[0] = None
                res_conn.send({"ok": True, "info": _plugin_info(plugin, path)})
            elif cmd == "process":
                y = _process(plugin, msg["audio"], msg["sr"], int(msg.get("warmup", 0)))
                res_conn.send({"ok": True, "audio": y, "latency": _plugin_info(plugin, path)["latency"]})
            elif cmd == "params":
                res_conn.send({"ok": True, "params": _params(plugin)})
            elif cmd == "set_param":
                plugin.parameters[msg["key"]].raw_value = float(msg["raw_value"])
                p = plugin.parameters[msg["key"]]
                res_conn.send({"ok": True, "text": str(getattr(p, "string_value", ""))})
            elif cmd == "state":
                state = {"ok": True}
                for attr in ("raw_state", "preset_data"):
                    try:
                        v = getattr(plugin, attr)
                        if v:
                            state[attr] = bytes(v)
                    except Exception:
                        pass
                res_conn.send(state)
            else:
                res_conn.send({"ok": False, "error": f"unknown command {cmd}"})
        except Exception as e:
            res_conn.send({"ok": False, "error": _explain(e, path), "trace": traceback.format_exc()})


def _explain(e: Exception, path: str) -> str:
    text = str(e) or type(e).__name__
    low = text.lower()
    if "shell" in low or "waveshell" in path.lower():
        return (f"{Path(path).name} is a shell plugin (like WaveShell), which can't be loaded directly. "
                "Use the DAW route for it.")
    if "contains" in low and "plugins" in low:
        return text + " Choose one of them."
    if "unable to load" in low or "could not" in low or "failed" in low:
        return (f"{Path(path).name} could not be loaded: {text}. It may be a 32-bit/incompatible build, need "
                "an authorization, or be a shell plugin. Try the DAW route.")
    return text


# --- parent side -------------------------------------------------------------------------


class PluginHostBackend(CaptureBackend):
    source_kind = "plugin"
    name = "Use plugin directly"

    def __init__(self):
        self._proc = None
        self._cmd = None
        self._res = None
        self._call_lock = threading.Lock()
        self._send_lock = threading.Lock()
        self.info: dict = {}
        self.last_determinism: CheckResult | None = None
        self.last_rendered: RenderedAudio | None = None
        self._last_load: dict | None = None
        self.crashes = 0

    # lifecycle
    def start(self) -> None:
        if self._proc is not None and self._proc.is_alive():
            return
        ctx = mp.get_context("spawn")
        cmd_r, cmd_w = ctx.Pipe(duplex=False)
        res_r, res_w = ctx.Pipe(duplex=False)
        self._proc = ctx.Process(target=worker_main, args=(cmd_r, res_w), daemon=True, name="irene-plugin-host")
        self._proc.start()
        self._cmd, self._res = cmd_w, res_r
        self.info = {}

    def stop(self) -> None:
        if self._proc is None:
            return
        try:
            with self._send_lock:
                self._cmd.send({"cmd": "quit"})
            self._proc.join(2)
        except (OSError, EOFError, BrokenPipeError):
            pass
        if self._proc.is_alive():
            self._proc.terminate()
            self._proc.join(2)
        self._proc = None

    def __enter__(self):
        self.start()
        return self

    def __exit__(self, *exc):
        self.stop()

    @property
    def alive(self) -> bool:
        return self._proc is not None and self._proc.is_alive()

    def _call(self, msg: dict, timeout: float | None = 600.0) -> dict:
        restarted = not self.alive
        self.start()
        if restarted and self._last_load and msg.get("cmd") not in ("load", "names", "quit"):
            # The worker died earlier (crashing plugin): load the plugin again transparently.
            self.info = self._call(dict(self._last_load), timeout=120)["info"]
        with self._call_lock:
            try:
                with self._send_lock:
                    self._cmd.send(msg)
                waited = 0.0
                while not self._res.poll(0.25):
                    waited += 0.25
                    if not self._proc.is_alive():
                        self._proc = None
                        self.crashes += 1
                        raise PluginHostCrashed(
                            "The plugin crashed the plugin host. irene-capture itself is fine; load the plugin "
                            "again (or try the DAW route if it keeps crashing).")
                    if timeout is not None and waited > timeout:
                        raise PluginError("The plugin did not respond in time.")
                reply = self._res.recv()
            except (EOFError, OSError, BrokenPipeError) as e:
                self._proc = None
                self.crashes += 1
                raise PluginHostCrashed(
                    "The plugin host stopped unexpectedly (the plugin crashed). irene-capture itself is fine; "
                    "the plugin is loaded again with its default settings on the next action.") from e
        if not reply.get("ok"):
            raise PluginError(reply.get("error", "Unknown plugin error"))
        return reply

    # commands
    def plugin_names(self, path: str | Path) -> list[str]:
        if str(path).startswith("builtin:"):
            return []
        return self._call({"cmd": "names", "path": str(path)})["names"]

    def load(self, path: str | Path, name: str | None = None) -> dict:
        msg = {"cmd": "load", "path": str(path), "name": name}
        self.info = self._call(msg, timeout=120)["info"]
        self._last_load = msg
        return self.info

    def show_editor(self, auto_close_s: float | None = None) -> dict:
        """Blocks until the editor is closed (by the user, :meth:`close_editor` or the timer)."""
        reply = self._call({"cmd": "show_editor", "auto_close_s": auto_close_s}, timeout=None)
        self.info = reply.get("info", self.info)
        return reply

    def close_editor(self) -> None:
        if self.alive:
            with self._send_lock:
                self._cmd.send({"cmd": "close_editor"})

    def parameters(self) -> list[dict]:
        return self._call({"cmd": "params"})["params"]

    def set_parameter(self, key: str, raw_value: float) -> str:
        return self._call({"cmd": "set_param", "key": key, "raw_value": raw_value})["text"]

    def state(self) -> dict:
        return self._call({"cmd": "state"})

    def process(self, audio: np.ndarray, sample_rate: int, warmup_s: float = WARMUP_S) -> np.ndarray:
        reply = self._call({"cmd": "process", "audio": audio.astype(np.float32), "sr": float(sample_rate),
                            "warmup": int(warmup_s * sample_rate)})
        self.info["latency"] = reply.get("latency", 0)
        return reply["audio"]

    # backend API
    def capture(self, session: Session, check_determinism: bool = True, **kwargs) -> RenderedAudio:
        x = render_test_signal(session)
        y = self.process(x, session.sample_rate)
        self.last_determinism = None
        if check_determinism:
            y2 = self.process(x, session.sample_rate)
            peak = float(np.max(np.abs(y))) or 1.0
            diff = db(float(np.max(np.abs(y - y2))) / peak) if y.shape == y2.shape else 0.0
            if diff > DETERMINISM_LIMIT_DB:
                self.last_determinism = CheckResult(
                    "determinism", Severity.WARNING, "Plugin is time-variant/random — IR may not match.",
                    f"Two identical runs differed by {diff:.0f} dB re peak. Turn off modulation, randomization, "
                    "analog noise or 'drift' features.")
            else:
                self.last_determinism = CheckResult("determinism", Severity.OK, "Plugin output is repeatable.")
        name = self.info.get("name", "plugin")
        rendered = RenderedAudio(y, session.sample_rate, "FLOAT", None, name)
        self.last_rendered = rendered
        return rendered

    def extra_checks(self) -> list[CheckResult]:
        return [self.last_determinism] if self.last_determinism else []

    def capture_and_analyze(self, session: Session, options: AnalysisOptions | None = None) -> CaptureResult:
        rendered = self.capture(session)
        opts = options or AnalysisOptions()
        opts.source_kind = "plugin"
        opts.extra_checks = [*opts.extra_checks, *self.extra_checks()]
        if self.info.get("latency"):
            opts.extra_checks.append(CheckResult(
                "plugin_latency", Severity.INFO,
                f"The plugin reports {self.info['latency']} samples of latency; it was compensated."))
        result = analyze(session, rendered, opts)
        result.plugin_info = dict(self.info)
        return result

    def state_attachments(self) -> dict[str, bytes]:
        """Plugin state as files to write next to the IR (``.vstpreset`` for VST3, raw state)."""
        try:
            st = self.state()
        except PluginError:
            return {}
        out = {}
        if st.get("preset_data") and self.info.get("format") == "VST3":
            out["_plugin.vstpreset"] = st["preset_data"]
        if st.get("raw_state"):
            out["_plugin_state.bin"] = st["raw_state"]
        return out

    def save_state(self, ir_path: Path) -> list[Path]:
        """Save the plugin state next to the IR, if the plugin exposes it."""
        stem = Path(ir_path).with_suffix("")
        out = []
        for suffix, data in self.state_attachments().items():
            p = stem.with_name(stem.name + suffix)
            p.write_bytes(data)
            out.append(p)
        return out
