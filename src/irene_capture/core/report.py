"""Write the IR file(s) and the reports (``_ir.json`` machine-readable, ``_ir.txt`` for people)."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

import numpy as np

from .. import TOOL_NAME, __version__
from .audio_io import write_audio
from .checks import Severity
from .presets import PRESETS, BitDepth, StereoMode, TrueStereoOutput
from .result import CaptureResult


@dataclass
class OutputFiles:
    ir_files: list[Path] = field(default_factory=list)
    json_file: Path | None = None
    txt_file: Path | None = None
    extra: list[Path] = field(default_factory=list)

    @property
    def all(self) -> list[Path]:
        return [*self.ir_files, *(p for p in (self.json_file, self.txt_file) if p), *self.extra]


def safe_name(name: str) -> str:
    name = re.sub(r"[^\w\-. ]+", "_", name, flags=re.UNICODE).strip(" ._")
    return re.sub(r"\s+", "_", name) or "capture"


def default_base(result: CaptureResult) -> str:
    """``<rendered basename>`` (DAW route) or ``<plugin>_<capture name>`` (direct route)."""
    if result.plugin_info:
        plugin = result.plugin_info.get("name", "plugin")
        cap = result.session.settings.capture_name or result.session.settings.capture_type.value
        return safe_name(f"{plugin}_{cap}")
    return safe_name(result.source_name)


def short_comment(result: CaptureResult) -> str:
    s = result.session.settings
    nt = f", null {result.null_test.residual_db:.0f} dB" if result.null_test else ""
    return (f"{TOOL_NAME} {__version__}: {PRESETS[s.capture_type].label}, {s.method.value}, "
            f"{s.sample_rate / 1000:g} kHz, norm {result.normalization.value}{nt}")


def plan_paths(result: CaptureResult, out_dir: Path, base: str) -> tuple[list[Path], Path, Path]:
    """IR, JSON and TXT paths that don't overwrite anything (``_2``, ``_3``, … appended)."""
    s = result.session.settings
    two_files = s.stereo_mode == StereoMode.TRUE_STEREO and s.true_stereo_output == TrueStereoOutput.TWO_FILES \
        and result.ir is not None and result.ir.shape[0] == 2
    i = 1
    while True:
        stem = f"{base}_ir" if i == 1 else f"{base}_ir_{i}"
        if two_files:
            irs = [out_dir / f"{stem}_inL.wav", out_dir / f"{stem}_inR.wav"]
        else:
            irs = [out_dir / f"{stem}.wav"]
        js, txt = out_dir / f"{stem}.json", out_dir / f"{stem}.txt"
        if not any(p.exists() for p in (*irs, js, txt)):
            return irs, js, txt
        i += 1


def write_outputs(result: CaptureResult, out_dir: str | Path | None = None, base: str | None = None) -> OutputFiles:
    if not result.ok:
        raise ValueError("The IR can't be created because of errors in the checks.")
    if out_dir is None:
        out_dir = result.source_path.parent if result.source_path else Path.cwd()
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    irs, js, txt = plan_paths(result, out_dir, base or default_base(result))
    comment = short_comment(result)
    software = f"{TOOL_NAME} {__version__}"
    files = OutputFiles()
    if len(irs) == 2:
        for k, p in enumerate(irs):
            write_audio(p, result.ir[k].T, result.sample_rate, result.ir_bit_depth, comment, software)
            files.ir_files.append(p)
    else:
        write_audio(irs[0], result.ir_channels().T, result.sample_rate, result.ir_bit_depth, comment, software)
        files.ir_files.append(irs[0])
    stem = js.with_suffix("")
    for suffix, data in result.attachments.items():
        p = stem.with_name(stem.name + suffix)
        p.write_bytes(data)
        files.extra.append(p)
    if result.rendered_to_save is not None:
        r = result.rendered_to_save
        p = stem.with_name(stem.name + "_rendered.wav")
        write_audio(p, r.data, r.sample_rate, BitDepth.FLOAT32, comment, software)
        files.extra.append(p)
    js.write_text(json.dumps(report_dict(result, files), indent=2, ensure_ascii=False), encoding="utf-8")
    txt.write_text(report_text(result, files), encoding="utf-8")
    files.json_file, files.txt_file = js, txt
    result.written = files.all
    return files


def _r(x, nd=3):
    return None if x is None else round(float(x), nd)


def report_dict(result: CaptureResult, files: OutputFiles | None = None) -> dict:
    s = result.session
    sr = result.sample_rate
    d = result.distortion
    return {
        "tool": TOOL_NAME,
        "tool_version": __version__,
        "created": datetime.now(UTC).isoformat(timespec="seconds"),
        "session_id": s.session_id,
        "short_id": s.short_id,
        "session_tool_version": s.tool_version,
        "source": str(result.source_path) if result.source_path else result.source_name,
        "plugin": {k: v for k, v in result.plugin_info.items() if isinstance(v, (str, int, float, bool))} or None,
        "ir_files": [p.name for p in files.ir_files] if files else [],
        "other_files": [p.name for p in files.extra] if files else [],
        "settings": s.settings.to_dict(),
        "effective": {
            "trim_latency": result.trim_latency,
            "tail_trim": result.tail_trim,
            "normalization": result.normalization.value,
            "ir_bit_depth": result.ir_bit_depth.value,
        },
        "checks": [c.to_dict() for c in result.checks],
        "conversions": [c.message for c in result.conversions],
        "channels": result.channel_names,
        "sample_rate": sr,
        "offset_samples": result.offset_samples,
        "realigned_samples": result.realigned_samples,
        "ir_start_samples": result.ir_start,
        "trimmed_latency_samples": result.latency_trimmed_samples,
        "trimmed_latency_ms": _r(result.latency_trimmed_samples / sr * 1000),
        "normalization_gain_db": _r(result.normalization_gain_db),
        "length_samples": result.length_samples,
        "length_s": _r(result.length_samples / sr, 4),
        "tail": None if result.tail is None else {
            "length_samples": result.tail.length,
            "threshold_db": _r(result.tail.threshold_db, 1),
            "noise_floor_db": _r(result.tail.noise_floor_db, 1),
            "end_level_db": _r(result.tail.end_level_db, 1),
            "truncated": bool(result.tail.truncated),
            "fade_samples": result.tail.fade_len,
        },
        "noise_floor_dbfs": _r(result.noise_floor_dbfs, 1),
        "distortion": None if d is None else {
            "thd_percent": _r(d.thd_percent, 4),
            "harmonics_db": {str(k): _r(v, 1) for k, v in d.harmonics_db.items()},
        },
        "rt60_s": _r(result.rt60_s),
        "null_test": result.null_test.to_dict() if result.null_test else None,
        "capture_null_test": result.capture_null.to_dict() if result.capture_null else None,
        "rating": result.rating,
        "hints": result.hints,
    }


SYMBOL = {Severity.OK: "OK  ", Severity.INFO: "INFO", Severity.WARNING: "WARN", Severity.ERROR: "ERR "}


def report_text(result: CaptureResult, files: OutputFiles | None = None) -> str:
    s = result.session.settings
    sr = result.sample_rate
    lines = [
        f"{TOOL_NAME} {__version__} — capture report",
        "",
        f"Source:      {result.source_path or result.source_name}",
    ]
    if result.plugin_info:
        lines.append(f"Plugin:      {result.plugin_info.get('name', '')} ({result.plugin_info.get('path', '')})")
    if files:
        lines.append("IR file(s):  " + ", ".join(p.name for p in files.ir_files))
    lines += [
        f"Type:        {PRESETS[s.capture_type].label}, {s.method.value}, {s.sample_rate / 1000:g} kHz, "
        f"{s.stereo_mode.value.replace('_', ' ')}",
        f"Channels:    {', '.join(result.channel_names)}",
        f"Length:      {result.length_samples / sr:.3f} s ({result.length_samples} samples)",
        f"Latency trim: {result.latency_trimmed_samples} samples; IR starts at {result.ir_start} samples",
        f"Normalization: {result.normalization.value} ({result.normalization_gain_db:+.2f} dB)",
    ]
    if result.distortion:
        lines.append(f"Distortion:  ≈ {result.distortion.thd_percent:.3f} % THD")
    if result.rt60_s:
        lines.append(f"RT60:        ≈ {result.rt60_s:.2f} s")
    if result.null_test:
        nt = result.null_test
        lines.append(f"Null test:   {nt.residual_db:.1f} dB — {nt.rating}")
        if nt.bands_db:
            lines.append("             " + ", ".join(f"{k}: {v:.1f} dB" for k, v in nt.bands_db.items()))
    else:
        lines.append("Null test:   skipped")
    lines += ["", "Checks:"]
    lines += [f"  [{SYMBOL[c.severity]}] {c.message}" for c in result.checks]
    if result.hints:
        lines += ["", "Hints:"] + [f"  - {h}" for h in result.hints]
    lines.append("")
    return "\n".join(lines)


def ir_preview(result: CaptureResult) -> np.ndarray:
    return result.ir_channels()
