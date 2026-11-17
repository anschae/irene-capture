"""Command line interface (same core as the GUI).

    irene-capture generate --type reverb --out ~/captures/
    irene-capture analyze rendered.wav [--session test.wav.capture.json]
    irene-capture plugin-capture --plugin /path/Plugin.vst3 --type cabinet
    irene-capture --selftest
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import TOOL_NAME, build_info
from .core import AnalysisOptions, CaptureSettings, NeedsDecision, analyze, create_session, load_session
from .core.analysis import Cancelled
from .core.audio_io import read_audio
from .core.checks import Severity
from .core.presets import (
    SAMPLE_RATES,
    BitDepth,
    CaptureType,
    Method,
    Normalization,
    StereoMode,
    TrueStereoOutput,
    validate,
)
from .core.report import report_dict, report_text, write_outputs

ICON = {Severity.OK: "✓", Severity.INFO: "i", Severity.WARNING: "!", Severity.ERROR: "✗"}


def _bool_flag(p: argparse.ArgumentParser, name: str, help_: str) -> None:
    p.add_argument(f"--{name}", dest=name.replace("-", "_"), action=argparse.BooleanOptionalAction, default=None,
                   help=help_)


def add_settings_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("--type", default="cabinet", choices=[t.value for t in CaptureType], help="capture type (preset)")
    p.add_argument("--method", default="sweep", choices=[m.value for m in Method])
    p.add_argument("--rate", type=int, default=48000, choices=SAMPLE_RATES, help="sample rate in Hz")
    p.add_argument("--ir-length", type=float, help="IR length in seconds (default: from the preset)")
    p.add_argument("--stereo-mode", choices=[m.value for m in StereoMode], help="default: from the preset")
    p.add_argument("--true-stereo-output", choices=[m.value for m in TrueStereoOutput], default="two_files")
    p.add_argument("--sweep-length", type=float, default=6.0)
    p.add_argument("--f-start", type=float, default=20.0)
    p.add_argument("--f-end", type=float, default=None, help="default: 0.95 × Nyquist")
    p.add_argument("--level", type=float, default=-6.0, help="test level in dBFS peak")
    p.add_argument("--test-bits", choices=[b.value for b in BitDepth], default="float32")
    p.add_argument("--ir-bits", choices=[b.value for b in BitDepth], default="pcm24")
    _bool_flag(p, "trim-latency", "trim latency (default: from the preset)")
    _bool_flag(p, "tail-trim", "automatic tail trim (default: on)")
    p.add_argument("--normalization", choices=[n.value for n in Normalization], help="default: from the preset")
    p.add_argument("--peak-target", type=float, default=-1.0)
    p.add_argument("--name", default="", help="capture name (used for direct-route file names)")


def settings_from_args(a: argparse.Namespace) -> CaptureSettings:
    over: dict = {
        "method": Method(a.method), "sample_rate": a.rate, "sweep_length_s": a.sweep_length,
        "f_start_hz": a.f_start, "f_end_hz": a.f_end, "test_level_dbfs": a.level,
        "test_bit_depth": BitDepth(a.test_bits), "ir_bit_depth": BitDepth(a.ir_bits),
        "peak_target_dbfs": a.peak_target, "true_stereo_output": TrueStereoOutput(a.true_stereo_output),
        "capture_name": a.name,
    }
    if a.ir_length is not None:
        over["ir_length_s"] = a.ir_length
    if a.stereo_mode:
        over["stereo_mode"] = StereoMode(a.stereo_mode)
    if a.trim_latency is not None:
        over["trim_latency"] = a.trim_latency
    if a.tail_trim is not None:
        over["tail_trim"] = a.tail_trim
    if a.normalization:
        over["normalization"] = Normalization(a.normalization)
    return CaptureSettings.for_type(a.type, **over)


def print_checks(result, out=None) -> None:
    out = out or sys.stdout
    for c in result.checks:
        print(f"  {ICON[c.severity]} {c.message}", file=out)
        if c.detail and c.severity in (Severity.WARNING, Severity.ERROR):
            print(f"      {c.detail}", file=out)
    for h in result.hints:
        print(f"  → {h}", file=out)


def cmd_generate(a: argparse.Namespace) -> int:
    from .backends.daw import DawRoundtripBackend, SessionIndex

    settings = settings_from_args(a)
    for m in validate(settings):
        print(f"{ICON[m.severity]} {m.text}")
    try:
        session = create_session(settings)
    except ValueError as e:
        print(f"✗ {e}", file=sys.stderr)
        return 2
    out = Path(a.out).expanduser()
    path = out / session.default_test_filename() if out.is_dir() or not out.suffix else out
    files = DawRoundtripBackend(SessionIndex()).create_test_file(session, path)
    print(f"Test file:     {files[0]}")
    print(f"Session file:  {files[1]}  (keep it next to the test file)")
    print(f"Instructions:  {files[2]}")
    return 0


def _analyze_one(path: Path, a: argparse.Namespace) -> int:
    from .backends.daw import SessionIndex, find_session

    if a.session:
        session, how = load_session(a.session), f"Session from {a.session}"
    else:
        session, how = find_session(path, SessionIndex())
        if session is None:
            print(f"✗ {path.name}: {how} (use --session)", file=sys.stderr)
            return 2
    print(f"{path.name}: {how}")
    rendered = read_audio(path)
    decisions = {}
    if a.channel:
        decisions["channel"] = a.channel
    if a.accept_mono:
        decisions["accept_mono"] = "continue"
    try:
        result = analyze(session, rendered, AnalysisOptions(decisions=decisions))
    except NeedsDecision as nd:
        flag = "--channel left|right|sum" if nd.decision.key == "channel" else "--accept-mono"
        print(f"? {nd.decision.question}\n  Run again with {flag}.", file=sys.stderr)
        return 3
    except Cancelled:
        return 1
    print_checks(result)
    if a.json:
        print(json.dumps(report_dict(result), indent=2, ensure_ascii=False))
    if not result.ok:
        print("✗ No IR written because of the errors above.", file=sys.stderr)
        return 1
    if a.dry_run:
        print(report_text(result))
        return 0
    files = write_outputs(result, a.out_dir)
    for p in files.all:
        print(f"  wrote {p}")
    return 0


def cmd_analyze(a: argparse.Namespace) -> int:
    rc = 0
    for f in a.files:
        rc = max(rc, _analyze_one(Path(f), a))
    return rc


def cmd_plugin_capture(a: argparse.Namespace) -> int:
    from .backends.plugin_host import PluginHostBackend

    settings = settings_from_args(a)
    session = create_session(settings, source="plugin")
    with PluginHostBackend() as host:
        info = host.load(a.plugin, a.plugin_name)
        print(f"Loaded {info['name']} (latency {info.get('latency', 0)} samples)")
        if a.editor or a.editor_seconds:
            res = host.show_editor(auto_close_s=a.editor_seconds)
            print(f"Editor closed ({'ok' if res.get('ok') else res})")
        result = host.capture_and_analyze(session)
        print_checks(result)
        if not result.ok:
            return 1
        files = write_outputs(result, a.out_dir or Path.cwd())
        files.extra += host.save_state(files.ir_files[0])
        for p in files.all:
            print(f"  wrote {p}")
    return 0


def cmd_list_plugins(a: argparse.Namespace) -> int:
    from .backends.plugin_host import scan_plugins

    for p in scan_plugins([Path(d) for d in a.folders]):
        print(p)
    return 0


def cmd_spike_editor(a: argparse.Namespace) -> int:
    """Open a plugin editor in the worker and close it after a few seconds (CI spike)."""
    from .backends.plugin_host import PluginHostBackend

    with PluginHostBackend() as host:
        info = host.load(a.plugin, a.plugin_name)
        print(f"Loaded {info['name']}; editor: {info.get('has_editor')}")
        res = host.show_editor(auto_close_s=a.seconds)
        print(f"Editor result: {res}")
        return 0 if res.get("ok") else 1


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog=TOOL_NAME, description="Capture impulse responses from audio plugins.")
    p.add_argument("--version", action="version", version=f"{TOOL_NAME} {build_info()}")
    p.add_argument("--selftest", action="store_true", help="generate → simulate → analyze, headless")
    p.add_argument("--log", help="also write output to this file (useful with the windowed .exe)")
    sub = p.add_subparsers(dest="cmd")

    g = sub.add_parser("generate", help="create a test file + session file + instructions")
    add_settings_args(g)
    g.add_argument("--out", default=".", help="folder or file name")
    g.set_defaults(func=cmd_generate)

    an = sub.add_parser("analyze", help="analyze rendered file(s) and write the IR")
    an.add_argument("files", nargs="+")
    an.add_argument("--session", help="the .capture.json file (default: found automatically)")
    an.add_argument("--channel", choices=["left", "right", "sum"], help="for mono captures of differing channels")
    an.add_argument("--accept-mono", action="store_true", help="accept a mono render of a stereo capture")
    an.add_argument("--out-dir", help="default: next to the rendered file")
    an.add_argument("--dry-run", action="store_true", help="don't write files")
    an.add_argument("--json", action="store_true", help="print the JSON report")
    an.set_defaults(func=cmd_analyze)

    pc = sub.add_parser("plugin-capture", help="capture a plugin directly (no DAW)")
    add_settings_args(pc)
    pc.add_argument("--plugin", required=True, help="path to .vst3/.component, or builtin:Name[:param=value,...]")
    pc.add_argument("--plugin-name", help="for files that contain several plugins")
    pc.add_argument("--editor", action="store_true", help="open the plugin editor first (close it to capture)")
    pc.add_argument("--editor-seconds", type=float, help="open the editor and close it after N seconds")
    pc.add_argument("--out-dir")
    pc.set_defaults(func=cmd_plugin_capture)

    lp = sub.add_parser("list-plugins", help="list plugins in the standard folders")
    lp.add_argument("folders", nargs="*")
    lp.set_defaults(func=cmd_list_plugins)

    sp = sub.add_parser("spike-editor", help="open and auto-close a plugin editor (diagnostics)")
    sp.add_argument("--plugin", required=True)
    sp.add_argument("--plugin-name")
    sp.add_argument("--seconds", type=float, default=3.0)
    sp.set_defaults(func=cmd_spike_editor)
    return p


class _Tee:
    def __init__(self, *streams):
        self.streams = [s for s in streams if s is not None]

    def write(self, data):
        for s in self.streams:
            s.write(data)
            s.flush()
        return len(data)

    def flush(self):
        for s in self.streams:
            s.flush()


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    log = open(args.log, "w", encoding="utf-8") if args.log else None  # noqa: SIM115
    if log:
        sys.stdout = _Tee(sys.stdout, log)
        sys.stderr = _Tee(sys.stderr, log)
    try:
        if args.selftest:
            from .selftest import run_selftest
            return run_selftest()
        if not getattr(args, "func", None):
            build_parser().print_help()
            return 0
        return args.func(args)
    finally:
        if log:
            sys.stdout.flush()
            log.close()
