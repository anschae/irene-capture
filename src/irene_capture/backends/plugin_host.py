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
LOAD_TIMEOUT_S = 120.0
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


def plugin_path_candidates(path: str) -> list[str]:
    """Paths to try with pedalboard, in order.

    On Windows, pedalboard (0.9.x) often can't scan VST3 *bundle folders* ("unsupported plugin
    format or scan failure"), but loads the module inside the bundle
    (``X.vst3/Contents/x86_64-win/X.vst3``) — found in the CI plugin spike with Dragonfly
    Reverb and Surge XT. Some plugins may need the bundle path, so it is tried first.
    """
    p = Path(path)
    if sys.platform != "win32" or not p.is_dir() or p.suffix.lower() != ".vst3":
        return [path]
    import platform

    arch = {"amd64": "x86_64-win", "x86_64": "x86_64-win", "arm64": "arm64x-win"}.get(platform.machine().lower(),
                                                                                       "x86_64-win")
    out = [path]
    for c in [p / "Contents" / arch / p.name, *sorted((p / "Contents").glob("*-win/*.vst3"))]:
        if c.is_file() and str(c) not in out:
            out.append(str(c))
    return out


def resolve_plugin_path(path: str) -> str:
    """The module inside a Windows VST3 bundle (or ``path`` itself)."""
    c = plugin_path_candidates(path)
    return c[1] if len(c) > 1 else c[0]


def _is_scan_failure(e: Exception) -> bool:
    low = str(e).lower()
    return "scan failure" in low or "unsupported plugin format" in low


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


def _is_external(plugin) -> bool:
    # Don't use hasattr(plugin, "raw_state"): that evaluates the property and asks the plugin
    # for its whole state, which some plugins are slow (or hang) with.
    return hasattr(type(plugin), "raw_state")


def _plugin_info(plugin, path: str) -> dict:
    name = getattr(plugin, "name", None) or type(plugin).__name__
    return {
        "name": str(name),
        "path": path,
        "manufacturer": str(getattr(plugin, "manufacturer_name", "") or ""),
        "version": str(getattr(plugin, "version", "") or ""),
        "latency": int(getattr(plugin, "reported_latency_samples", 0) or 0),
        "has_editor": hasattr(type(plugin), "show_editor"),
        "is_external": _is_external(plugin),
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


def worker_main(cmd_conn, res_conn, log_path: str | None = None) -> None:  # pragma: no cover - child process
    """The plugin host process. Never raises: a broken pipe means the app is gone, so it just exits."""
    import faulthandler
    import os
    import time

    log = None
    if log_path:
        try:
            log = open(log_path, "a", encoding="utf-8", buffering=1)  # noqa: SIM115
            # A windowed build has no stdout/stderr; send everything (and native crash dumps) to the log.
            sys.stdout = sys.stderr = log
            faulthandler.enable(log)
        except OSError:
            log = None
    if sys.stdout is None or sys.stderr is None:
        sys.stdout = sys.stderr = open(os.devnull, "w")  # noqa: SIM115

    def note(text: str) -> None:
        if log:
            log.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} [{os.getpid()}] {text}\n")

    def send(obj) -> None:
        try:
            res_conn.send(obj)
        except (BrokenPipeError, EOFError, OSError):
            note("app is gone (pipe closed); exiting")
            os._exit(0)

    try:
        _worker_loop(cmd_conn, send, note)
    except BaseException as e:  # never show an "unhandled exception" dialog from the worker
        note(f"worker failed: {e!r}\n{traceback.format_exc()}")
        os._exit(1)
    os._exit(0)


def _load_external(pedalboard, path: str, name: str | None, note) -> object:
    kwargs = {"plugin_name": name} if name else {}
    errors = []
    for candidate in plugin_path_candidates(path):
        note(f"load {candidate!r} {kwargs}")
        try:
            plugin = pedalboard.load_plugin(candidate, **kwargs)
            note("load ok")
            return plugin
        except Exception as e:
            note(f"load failed: {e}")
            errors.append(e)
            if not _is_scan_failure(e):
                break
    raise errors[-1]


def _plugin_names(pedalboard, path: str, note) -> list[str]:
    cls = pedalboard.VST3Plugin if path.lower().endswith(".vst3") else pedalboard.AudioUnitPlugin
    err = None
    for candidate in plugin_path_candidates(path):
        note(f"names {candidate!r}")
        try:
            return list(cls.get_plugin_names_for_file(candidate))
        except Exception as e:
            note(f"names failed: {e}")
            err = e
            if not _is_scan_failure(e):
                break
    raise err


def _worker_loop(cmd_conn, send, note) -> None:  # pragma: no cover - child process
    import os

    note(f"worker started (python {sys.version.split()[0]})")
    import pedalboard

    note(f"pedalboard {pedalboard.__version__}")
    inbox: queue.Queue = queue.Queue()
    editor_event: list[threading.Event | None] = [None]

    def reader():
        while True:
            try:
                msg = cmd_conn.recv()
            except (EOFError, OSError):
                # The app closed the pipe (quit, timeout or crash). Leave at once, even if the main
                # thread is stuck inside a plugin.
                note("command pipe closed; exiting")
                os._exit(0)
            if msg.get("cmd") == "close_editor":
                ev = editor_event[0]
                if ev is not None:
                    ev.set()
                continue
            inbox.put(msg)

    threading.Thread(target=reader, daemon=True).start()
    send({"ok": True, "hello": os.getpid()})
    plugin = None
    path = ""
    while True:
        msg = inbox.get()
        cmd = msg.get("cmd")
        if cmd != "process":
            note(f"command {cmd}")
        try:
            if cmd == "quit":
                send({"ok": True})
                return
            if cmd == "names":
                send({"ok": True, "names": _plugin_names(pedalboard, msg["path"], note)})
            elif cmd == "load":
                path = msg["path"]
                if path.startswith("builtin:"):
                    plugin = _make_builtin(path)
                else:
                    plugin = _load_external(pedalboard, path, msg.get("name"), note)
                    if getattr(plugin, "is_instrument", False) and not getattr(plugin, "is_effect", True):
                        raise PluginError("This is an instrument, not an effect.")
                send({"ok": True, "info": _plugin_info(plugin, path)})
            elif plugin is None:
                send({"ok": False, "error": "No plugin loaded."})
            elif cmd == "show_editor":
                if not hasattr(type(plugin), "show_editor"):
                    send({"ok": False, "error": "This plugin has no editor."})
                    continue
                ev = threading.Event()
                editor_event[0] = ev
                if msg.get("auto_close_s"):
                    threading.Timer(float(msg["auto_close_s"]), ev.set).start()
                plugin.show_editor(ev)
                editor_event[0] = None
                note("editor closed")
                send({"ok": True, "info": _plugin_info(plugin, path)})
            elif cmd == "process":
                y = _process(plugin, msg["audio"], msg["sr"], int(msg.get("warmup", 0)))
                send({"ok": True, "audio": y, "latency": _plugin_info(plugin, path)["latency"]})
            elif cmd == "params":
                send({"ok": True, "params": _params(plugin)})
            elif cmd == "set_param":
                plugin.parameters[msg["key"]].raw_value = float(msg["raw_value"])
                p = plugin.parameters[msg["key"]]
                send({"ok": True, "text": str(getattr(p, "string_value", ""))})
            elif cmd == "state":
                state = {"ok": True}
                for attr in ("raw_state", "preset_data"):
                    try:
                        v = getattr(plugin, attr)
                        if v:
                            state[attr] = bytes(v)
                    except Exception:
                        pass
                send(state)
            else:
                send({"ok": False, "error": f"unknown command {cmd}"})
        except Exception as e:
            note(f"error: {e!r}")
            send({"ok": False, "error": _explain(e, path), "trace": traceback.format_exc()})


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
        self._worker_pid: int | None = None
        self.crashes = 0
        from ..paths import config_dir

        self.log_path = config_dir() / "plugin-host.log"

    # lifecycle
    def start(self) -> None:
        if self._proc is not None and self._proc.is_alive():
            return
        self._close_pipes()
        try:  # keep the log small: start a new one above 1 MB
            if self.log_path.exists() and self.log_path.stat().st_size > 1_000_000:
                self.log_path.replace(self.log_path.with_suffix(".old.log"))
        except OSError:
            pass
        ctx = mp.get_context("spawn")
        cmd_r, cmd_w = ctx.Pipe(duplex=False)
        res_r, res_w = ctx.Pipe(duplex=False)
        self._proc = ctx.Process(target=worker_main, args=(cmd_r, res_w, str(self.log_path)), daemon=True,
                                 name="irene-plugin-host")
        self._proc.start()
        cmd_r.close()  # the parent keeps only its own ends, so the worker sees EOF when we close ours
        res_w.close()
        self._cmd, self._res = cmd_w, res_r
        self.info = {}
        # The worker says hello with its own process ID. In a one-file Windows build the Process
        # object is the bootloader, and the real worker is its child: killing it needs this PID.
        self._worker_pid = None
        waited = 0.0
        while not self._res.poll(0.25):
            waited += 0.25
            if not self._proc.is_alive() or waited > 120:
                self._kill()
                raise PluginHostCrashed(
                    f"The plugin host could not be started. Details are in {self.log_path}.")
        try:
            self._worker_pid = int(self._res.recv().get("hello"))
        except (EOFError, OSError, TypeError, ValueError) as e:
            self._kill()
            raise PluginHostCrashed(f"The plugin host could not be started. Details are in {self.log_path}.") from e

    def _close_pipes(self) -> None:
        for conn in (self._cmd, self._res):
            if conn is not None:
                try:
                    conn.close()
                except OSError:
                    pass
        self._cmd = self._res = None

    def _kill(self) -> None:
        """Stop the worker for sure (also the real worker behind a one-file bootloader)."""
        import os
        import signal

        pid = getattr(self, "_worker_pid", None)
        if pid:
            try:
                os.kill(pid, signal.SIGTERM if sys.platform == "win32" else signal.SIGKILL)
            except OSError:
                pass
        if self._proc is not None:
            if self._proc.is_alive():
                self._proc.kill()
            self._proc.join(3)
        self._close_pipes()  # the worker's reader sees EOF and exits, should it still run
        self._proc = None
        self._worker_pid = None

    def stop(self) -> None:
        if self._proc is None:
            return
        try:
            with self._send_lock:
                self._cmd.send({"cmd": "quit"})
            self._proc.join(3)
        except (OSError, EOFError, BrokenPipeError, AttributeError):
            pass
        self._kill()

    def kill_worker(self) -> None:
        """Stop a stuck worker immediately (a running call then raises PluginHostCrashed)."""
        import os
        import signal

        pid = getattr(self, "_worker_pid", None)
        if pid:
            try:
                os.kill(pid, signal.SIGTERM if sys.platform == "win32" else signal.SIGKILL)
            except OSError:
                pass
        if self._proc is not None and self._proc.is_alive():
            self._proc.kill()

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
            self.info = self._call(dict(self._last_load), timeout=LOAD_TIMEOUT_S)["info"]
        with self._call_lock:
            try:
                with self._send_lock:
                    self._cmd.send(msg)
                waited = 0.0
                while not self._res.poll(0.25):
                    waited += 0.25
                    if not self._proc.is_alive():
                        self._kill()
                        self.crashes += 1
                        raise PluginHostCrashed(
                            "The plugin crashed the plugin host. irene-capture itself is fine; load the plugin "
                            f"again (or use the DAW route if it keeps crashing). Details: {self.log_path}")
                    if timeout is not None and waited > timeout:
                        # stop the stuck worker, so it doesn't keep a CPU core busy; the next
                        # action starts a new one (and reloads the plugin)
                        self._kill()
                        raise PluginError(
                            f"The plugin did not respond within {timeout:.0f} s and was stopped. Some plugins "
                            "(e.g. with copy protection or a first-start/activation window) can't be hosted "
                            f"directly — use the DAW route for them. Details: {self.log_path}")
                reply = self._res.recv()
            except (EOFError, OSError, BrokenPipeError, AttributeError) as e:
                self._kill()
                self.crashes += 1
                raise PluginHostCrashed(
                    "The plugin host stopped unexpectedly (the plugin crashed). irene-capture itself is fine; "
                    "the plugin is loaded again with its default settings on the next action. "
                    f"Details: {self.log_path}") from e
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
        self.info = self._call(msg, timeout=LOAD_TIMEOUT_S)["info"]
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
        return self._call({"cmd": "state"}, timeout=60)

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
