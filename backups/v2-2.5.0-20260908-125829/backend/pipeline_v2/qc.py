"""Non-blocking media quality-control reports for pipeline v2.

QC findings are diagnostic only in phase 2. A report always keeps
``blocking`` false and ``delivery_allowed`` true, even when errors are found.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple, Union

from .artifact_store import ArtifactStore
from .atomic_io import atomic_write_json
from .models import utc_now


PathLike = Union[str, os.PathLike]


@dataclass(frozen=True)
class QCSettings:
    duration_tolerance_seconds: float = 0.5
    duration_tolerance_ratio: float = 0.01
    silence_noise_db: float = -50.0
    silence_min_seconds: float = 2.0
    target_lufs_min: float = -16.0
    target_lufs_max: float = -14.0
    true_peak_max_dbtp: float = -1.0
    clipping_risk_db: float = -0.1
    subtitle_safe_margin_x_pct: float = 0.05
    subtitle_safe_margin_y_pct: float = 0.05
    sample_frames: bool = True
    command_timeout_seconds: float = 120.0


@dataclass(frozen=True)
class QCCheck:
    name: str
    status: str
    message: str
    metrics: Mapping[str, Any] = field(default_factory=dict)


@dataclass
class QCReport:
    video_path: str
    generated_at: str = field(default_factory=utc_now)
    mode: str = "report_only"
    blocking: bool = False
    delivery_allowed: bool = True
    overall: str = "ok"
    checks: List[QCCheck] = field(default_factory=list)
    media: Dict[str, Any] = field(default_factory=dict)
    metrics: Dict[str, Any] = field(default_factory=dict)
    diagnostic_artifacts: List[Dict[str, Any]] = field(default_factory=list)

    def add(
        self,
        name: str,
        status: str,
        message: str,
        metrics: Optional[Mapping[str, Any]] = None,
    ) -> None:
        if status not in {"pass", "info", "warning", "error", "skipped"}:
            raise ValueError("Unsupported QC status: {!r}".format(status))
        self.checks.append(
            QCCheck(name=name, status=status, message=message, metrics=metrics or {})
        )

    def finalize(self) -> None:
        self.overall = (
            "issues_found"
            if any(check.status in {"warning", "error"} for check in self.checks)
            else "ok"
        )
        # These phase-2 guarantees must not depend on findings.
        self.blocking = False
        self.delivery_allowed = True

    def to_dict(self) -> Dict[str, Any]:
        counts = {status: 0 for status in ("pass", "info", "warning", "error", "skipped")}
        for check in self.checks:
            counts[check.status] += 1
        return {
            "schema_version": 1,
            "generated_at": self.generated_at,
            "mode": self.mode,
            "blocking": self.blocking,
            "delivery_allowed": self.delivery_allowed,
            "overall": self.overall,
            "video_path": self.video_path,
            "summary": counts,
            "checks": [asdict(check) for check in self.checks],
            "media": self.media,
            "metrics": self.metrics,
            "diagnostic_artifacts": self.diagnostic_artifacts,
        }


@dataclass(frozen=True)
class QCGateDecision:
    allowed: bool
    policy: str
    reason: str
    blocking_checks: Sequence[str] = ()


def evaluate_qc_gate(report: Union[QCReport, Mapping[str, Any]], policy: Any) -> QCGateDecision:
    """Apply the phase-8 rollout policy without changing the QC evidence."""

    policy_value = str(getattr(policy, "value", policy)).lower()
    if isinstance(report, QCReport):
        checks = [asdict(check) for check in report.checks]
    else:
        checks = list(report.get("checks", []))
    errors = [
        str(check.get("name", "unknown"))
        for check in checks
        if check.get("status") == "error"
    ]
    if policy_value == "block" and errors:
        return QCGateDecision(
            False,
            policy_value,
            "QC gate blocked delivery because critical errors were reported",
            tuple(errors),
        )
    if errors:
        return QCGateDecision(
            True,
            policy_value,
            "QC errors were reported but rollout policy allows delivery",
            tuple(errors),
        )
    return QCGateDecision(True, policy_value, "QC gate allows delivery")


def _run_command(command: Sequence[str], timeout: float) -> subprocess.CompletedProcess:
    creation_flags = 0
    if os.name == "nt" and hasattr(subprocess, "CREATE_NO_WINDOW"):
        creation_flags = subprocess.CREATE_NO_WINDOW
    return subprocess.run(
        list(command),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        creationflags=creation_flags,
    )


def probe_media(
    path: PathLike,
    ffprobe_binary: str = "ffprobe",
    timeout: float = 30.0,
) -> Dict[str, Any]:
    command = [
        ffprobe_binary,
        "-v",
        "error",
        "-show_entries",
        (
            "format=duration,format_name,size,bit_rate:"
            "stream=index,codec_type,codec_name,width,height,duration,"
            "sample_rate,channels,channel_layout"
        ),
        "-of",
        "json",
        str(path),
    ]
    result = _run_command(command, timeout=timeout)
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or "ffprobe returned an error")
    return json.loads(result.stdout)


def _float_or_none(value: Any) -> Optional[float]:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _duration_seconds(probe: Mapping[str, Any]) -> Optional[float]:
    duration = _float_or_none(dict(probe.get("format", {})).get("duration"))
    if duration is not None:
        return duration
    durations = [
        value
        for value in (
            _float_or_none(stream.get("duration")) for stream in probe.get("streams", [])
        )
        if value is not None
    ]
    return max(durations) if durations else None


def _stream_duration_seconds(
    probe: Mapping[str, Any], codec_type: str
) -> Optional[float]:
    durations = [
        value
        for value in (
            _float_or_none(stream.get("duration"))
            for stream in probe.get("streams", [])
            if stream.get("codec_type") == codec_type
        )
        if value is not None
    ]
    return max(durations) if durations else _duration_seconds(probe)


def _streams(probe: Mapping[str, Any], kind: str) -> List[Mapping[str, Any]]:
    return [
        stream
        for stream in probe.get("streams", [])
        if stream.get("codec_type") == kind
    ]


def _analyse_audio(
    path: Path,
    settings: QCSettings,
    ffmpeg_binary: str,
) -> Tuple[Dict[str, Any], List[QCCheck]]:
    metrics: Dict[str, Any] = {}
    checks: List[QCCheck] = []
    loudness_command = [
        ffmpeg_binary,
        "-hide_banner",
        "-nostats",
        "-i",
        str(path),
        "-map",
        "0:a:0",
        "-af",
        "ebur128=peak=true",
        "-f",
        "null",
        "-",
    ]
    loudness = _run_command(loudness_command, settings.command_timeout_seconds)
    if loudness.returncode != 0:
        checks.append(
            QCCheck(
                "audio_loudness",
                "error",
                loudness.stderr.strip()[-1000:] or "FFmpeg loudness analysis failed",
            )
        )
    else:
        integrated_matches = re.findall(
            r"^\s*I:\s*(-?(?:\d+(?:\.\d+)?|inf))\s+LUFS",
            loudness.stderr,
            flags=re.MULTILINE | re.IGNORECASE,
        )
        peak_matches = re.findall(
            r"^\s*Peak:\s*(-?(?:\d+(?:\.\d+)?|inf))\s+dBFS",
            loudness.stderr,
            flags=re.MULTILINE | re.IGNORECASE,
        )
        integrated = _float_or_none(integrated_matches[-1]) if integrated_matches else None
        true_peak = _float_or_none(peak_matches[-1]) if peak_matches else None
        metrics.update({"integrated_lufs": integrated, "true_peak_dbtp": true_peak})
        if integrated is None:
            checks.append(
                QCCheck("audio_loudness", "warning", "Integrated loudness was not parsed")
            )
        elif settings.target_lufs_min <= integrated <= settings.target_lufs_max:
            checks.append(
                QCCheck(
                    "audio_loudness",
                    "pass",
                    "Integrated loudness is inside the target range",
                    {"integrated_lufs": integrated},
                )
            )
        else:
            checks.append(
                QCCheck(
                    "audio_loudness",
                    "warning",
                    "Integrated loudness is outside the -16 to -14 LUFS target",
                    {"integrated_lufs": integrated},
                )
            )

        if true_peak is None:
            checks.append(
                QCCheck("audio_true_peak", "warning", "True peak was not parsed")
            )
        elif true_peak >= settings.clipping_risk_db:
            checks.append(
                QCCheck(
                    "audio_true_peak",
                    "warning",
                    "Peak is close to 0 dBFS and may clip",
                    {"true_peak_dbtp": true_peak},
                )
            )
        elif true_peak > settings.true_peak_max_dbtp:
            checks.append(
                QCCheck(
                    "audio_true_peak",
                    "warning",
                    "True peak exceeds the -1 dBTP target",
                    {"true_peak_dbtp": true_peak},
                )
            )
        else:
            checks.append(
                QCCheck(
                    "audio_true_peak",
                    "pass",
                    "True peak is within target",
                    {"true_peak_dbtp": true_peak},
                )
            )

    silence_command = [
        ffmpeg_binary,
        "-hide_banner",
        "-nostats",
        "-i",
        str(path),
        "-map",
        "0:a:0",
        "-af",
        "silencedetect=noise={}dB:d={}".format(
            settings.silence_noise_db, settings.silence_min_seconds
        ),
        "-f",
        "null",
        "-",
    ]
    silence = _run_command(silence_command, settings.command_timeout_seconds)
    if silence.returncode != 0:
        checks.append(
            QCCheck(
                "long_silence",
                "error",
                silence.stderr.strip()[-1000:] or "FFmpeg silence analysis failed",
            )
        )
    else:
        durations = [
            float(value)
            for value in re.findall(r"silence_duration:\s*([0-9.]+)", silence.stderr)
        ]
        metrics["long_silence_count"] = len(durations)
        metrics["long_silence_total_seconds"] = round(sum(durations), 3)
        if durations:
            checks.append(
                QCCheck(
                    "long_silence",
                    "warning",
                    "Detected silence intervals at or above the configured duration",
                    {
                        "count": len(durations),
                        "total_seconds": round(sum(durations), 3),
                        "longest_seconds": round(max(durations), 3),
                    },
                )
            )
        else:
            checks.append(
                QCCheck("long_silence", "pass", "No unexpectedly long silence detected")
            )
    return metrics, checks


def _parse_srt_timestamp(value: str) -> float:
    match = re.fullmatch(r"(\d+):(\d{2}):(\d{2})[,.](\d{3})", value.strip())
    if not match:
        raise ValueError("Invalid SRT timestamp: {!r}".format(value))
    hours, minutes, seconds, milliseconds = (int(item) for item in match.groups())
    return hours * 3600 + minutes * 60 + seconds + milliseconds / 1000.0


def _load_segments(path: Path) -> List[Dict[str, Any]]:
    if path.suffix.lower() == ".json":
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
        if isinstance(payload, dict):
            payload = payload.get("segments", [])
        if not isinstance(payload, list):
            raise ValueError("Segment JSON must be a list or contain a segments list")
        return [dict(item) for item in payload]
    if path.suffix.lower() == ".srt":
        content = path.read_text(encoding="utf-8-sig").replace("\r\n", "\n")
        segments = []
        for block in re.split(r"\n\s*\n", content.strip()):
            lines = block.splitlines()
            if len(lines) < 3 or "-->" not in lines[1]:
                continue
            start, end = (item.strip() for item in lines[1].split("-->", 1))
            segments.append(
                {
                    "id": int(lines[0].strip()),
                    "start": _parse_srt_timestamp(start),
                    "end": _parse_srt_timestamp(end),
                    "text": "\n".join(lines[2:]).strip(),
                }
            )
        return segments
    raise ValueError("Segments must be supplied as .json or .srt")


def _check_segments(path: Path) -> Tuple[Dict[str, Any], List[QCCheck]]:
    segments = _load_segments(path)
    checks: List[QCCheck] = []
    metrics: Dict[str, Any] = {"segment_count": len(segments)}
    if not segments:
        return metrics, [QCCheck("segments", "warning", "No subtitle segments found")]

    invalid_ranges = []
    empty_text = []
    missing_audio = []
    timing_failures = []
    timing_overflow_seconds = {}
    untranslated_source = []
    timing_metadata_count = 0
    translation_metadata_count = 0
    numeric_ids = []
    for position, segment in enumerate(segments, 1):
        segment_id = segment.get("id", segment.get("index", position))
        try:
            numeric_ids.append(int(segment_id))
        except (TypeError, ValueError):
            pass
        start = _float_or_none(segment.get("start"))
        end = _float_or_none(segment.get("end"))
        if start is None or end is None or start < 0 or end <= start:
            invalid_ranges.append(segment_id)
        text = str(segment.get("text", segment.get("content", ""))).strip()
        if not text:
            empty_text.append(segment_id)
        audio_value = segment.get("audio_path")
        if audio_value:
            audio_path = Path(str(audio_value))
            if not audio_path.is_absolute():
                audio_path = path.parent / audio_path
            if not audio_path.is_file():
                missing_audio.append(segment_id)
        if "timing_fits" in segment:
            timing_metadata_count += 1
            actual_duration = _float_or_none(segment.get("actual_audio_duration"))
            target_duration = _float_or_none(segment.get("target_audio_duration"))
            if segment.get("timing_fits") is False:
                timing_failures.append(segment_id)
            if actual_duration is not None and target_duration is not None:
                overflow = actual_duration - target_duration
                if overflow > 0.08:
                    timing_overflow_seconds[str(segment_id)] = round(overflow, 3)
                    if segment_id not in timing_failures:
                        timing_failures.append(segment_id)
        original_text = str(segment.get("orig_content") or "").strip()
        if original_text:
            translation_metadata_count += 1
            if (
                original_text == text
                and any("\u4e00" <= character <= "\u9fff" for character in original_text)
            ):
                untranslated_source.append(segment_id)

    missing_ids = []
    if numeric_ids:
        unique_ids = set(numeric_ids)
        missing_ids = sorted(set(range(min(unique_ids), max(unique_ids) + 1)) - unique_ids)
    duplicate_ids = sorted(
        segment_id for segment_id in set(numeric_ids) if numeric_ids.count(segment_id) > 1
    )
    metrics.update(
        {
            "invalid_range_count": len(invalid_ranges),
            "empty_text_count": len(empty_text),
            "missing_audio_count": len(missing_audio),
            "missing_ids": missing_ids,
            "duplicate_ids": duplicate_ids,
            "timing_failure_count": len(timing_failures),
            "timing_failure_ids": timing_failures,
            "timing_overflow_seconds": timing_overflow_seconds,
            "untranslated_source_count": len(untranslated_source),
            "untranslated_source_ids": untranslated_source,
        }
    )
    problems = invalid_ranges or empty_text or missing_audio or missing_ids or duplicate_ids
    if problems:
        checks.append(
            QCCheck(
                "segments",
                "error",
                "Missing or invalid segment data was detected",
                metrics,
            )
        )
    else:
        checks.append(QCCheck("segments", "pass", "All segment records are complete", metrics))
    if timing_metadata_count == 0:
        checks.append(
            QCCheck("segment_timing", "skipped", "No measured audio timing metadata")
        )
    elif timing_failures:
        checks.append(
            QCCheck(
                "segment_timing",
                "error",
                "Measured dubbing audio exceeds one or more segment windows",
                {
                    "segment_ids": timing_failures,
                    "overflow_seconds": timing_overflow_seconds,
                },
            )
        )
    else:
        checks.append(
            QCCheck(
                "segment_timing",
                "pass",
                "All measured dubbing audio fits its segment window",
            )
        )
    if translation_metadata_count == 0:
        checks.append(
            QCCheck(
                "translation_fallback",
                "skipped",
                "No source translation metadata was supplied",
            )
        )
    elif untranslated_source:
        checks.append(
            QCCheck(
                "translation_fallback",
                "error",
                "Chinese source text remained unchanged in translated subtitles",
                {"segment_ids": untranslated_source},
            )
        )
    else:
        checks.append(
            QCCheck(
                "translation_fallback",
                "pass",
                "No unchanged Chinese source fallback was detected",
            )
        )
    return metrics, checks


def _check_ass_safe_area(
    path: Path, settings: QCSettings
) -> Tuple[Dict[str, Any], List[QCCheck]]:
    content = path.read_text(encoding="utf-8-sig", errors="replace")
    width_match = re.search(r"^PlayResX:\s*([0-9.]+)", content, re.MULTILINE)
    height_match = re.search(r"^PlayResY:\s*([0-9.]+)", content, re.MULTILINE)
    positions = [
        (float(x), float(y))
        for x, y in re.findall(
            r"\\pos\(\s*(-?[0-9.]+)\s*,\s*(-?[0-9.]+)\s*\)", content
        )
    ]
    metrics = {"position_count": len(positions)}
    if not width_match or not height_match:
        return metrics, [
            QCCheck(
                "subtitle_safe_area",
                "warning",
                "ASS PlayResX/PlayResY is missing; safe area could not be verified",
            )
        ]
    width = float(width_match.group(1))
    height = float(height_match.group(1))
    metrics.update({"play_res_x": width, "play_res_y": height})
    if not positions:
        return metrics, [
            QCCheck(
                "subtitle_safe_area",
                "info",
                "No explicit ASS position tags found; style-based placement was not estimated",
                metrics,
            )
        ]
    min_x = width * settings.subtitle_safe_margin_x_pct
    max_x = width * (1.0 - settings.subtitle_safe_margin_x_pct)
    min_y = height * settings.subtitle_safe_margin_y_pct
    max_y = height * (1.0 - settings.subtitle_safe_margin_y_pct)
    outside = [
        {"x": x, "y": y}
        for x, y in positions
        if x < min_x or x > max_x or y < min_y or y > max_y
    ]
    metrics["outside_anchor_count"] = len(outside)
    if outside:
        return metrics, [
            QCCheck(
                "subtitle_safe_area",
                "warning",
                "Some ASS position anchors are outside the configured safe area",
                {**metrics, "examples": outside[:10]},
            )
        ]
    return metrics, [
        QCCheck(
            "subtitle_safe_area",
            "pass",
            "ASS position anchors are inside the configured safe area",
            metrics,
        )
    ]


def _sample_frames(
    video_path: Path,
    duration: float,
    diagnostics_directory: Path,
    ffmpeg_binary: str,
    timeout: float,
) -> Tuple[List[Dict[str, Any]], List[QCCheck]]:
    store = ArtifactStore(diagnostics_directory)
    samples = (
        ("first", 0.0),
        ("middle", max(0.0, duration / 2.0)),
        ("last", max(0.0, duration - 0.1)),
    )
    artifacts: List[Dict[str, Any]] = []
    failures = []
    for label, timestamp in samples:
        key = "frames/{}.png".format(label)
        staged = store.staging_path(key)
        command = [
            ffmpeg_binary,
            "-hide_banner",
            "-loglevel",
            "error",
            "-ss",
            "{:.3f}".format(timestamp),
            "-i",
            str(video_path),
            "-frames:v",
            "1",
            "-vf",
            "scale='min(960,iw)':-2",
            "-vcodec",
            "png",
            "-f",
            "image2",
            "-y",
            str(staged),
        ]
        try:
            result = _run_command(command, timeout)
            if result.returncode != 0 or not staged.is_file() or staged.stat().st_size == 0:
                failures.append(label)
                try:
                    staged.unlink()
                except FileNotFoundError:
                    pass
                continue
            record = store.commit_staged(
                staged,
                key,
                metadata={"timestamp_seconds": round(timestamp, 3)},
            )
            artifacts.append(record.to_dict())
        except (OSError, subprocess.SubprocessError):
            failures.append(label)
            try:
                staged.unlink()
            except FileNotFoundError:
                pass
    if failures:
        return artifacts, [
            QCCheck(
                "frame_samples",
                "warning",
                "One or more diagnostic frames could not be extracted",
                {"failed": failures, "created": len(artifacts)},
            )
        ]
    return artifacts, [
        QCCheck(
            "frame_samples",
            "pass",
            "Created first, middle and last diagnostic frames",
            {"created": len(artifacts)},
        )
    ]


def run_report_only_qc(
    video_path: PathLike,
    report_path: PathLike,
    audio_path: Optional[PathLike] = None,
    segments_path: Optional[PathLike] = None,
    ass_path: Optional[PathLike] = None,
    diagnostics_directory: Optional[PathLike] = None,
    settings: Optional[QCSettings] = None,
    ffmpeg_binary: str = "ffmpeg",
    ffprobe_binary: str = "ffprobe",
) -> QCReport:
    """Collect QC diagnostics and atomically publish a non-blocking report."""

    config = settings or QCSettings()
    video = Path(video_path)
    report = QCReport(video_path=str(video))
    video_probe: Optional[Dict[str, Any]] = None

    if not video.is_file():
        report.add("video_file", "error", "Final video file is missing")
    else:
        report.add("video_file", "pass", "Final video file exists", {"size_bytes": video.stat().st_size})
        try:
            video_probe = probe_media(
                video,
                ffprobe_binary=ffprobe_binary,
                timeout=min(config.command_timeout_seconds, 30.0),
            )
            report.media["video"] = video_probe
            video_streams = _streams(video_probe, "video")
            audio_streams = _streams(video_probe, "audio")
            report.add(
                "video_stream",
                "pass" if video_streams else "error",
                "Final output has a video stream" if video_streams else "Final output has no video stream",
                {"count": len(video_streams)},
            )
            report.add(
                "audio_stream",
                "pass" if audio_streams else "error",
                "Final output has an audio stream" if audio_streams else "Final output has no audio stream",
                {"count": len(audio_streams)},
            )
        except (OSError, RuntimeError, ValueError, json.JSONDecodeError, subprocess.SubprocessError) as exc:
            report.add("media_probe", "error", "Could not probe final video: {}".format(exc))

    audio = Path(audio_path) if audio_path is not None else video
    if not audio.is_file():
        report.add("audio_file", "error", "Audio file for analysis is missing")
    else:
        audio_probe: Optional[Dict[str, Any]] = None
        try:
            audio_probe = probe_media(
                audio,
                ffprobe_binary=ffprobe_binary,
                timeout=min(config.command_timeout_seconds, 30.0),
            )
            report.media["audio_analysis_source"] = audio_probe
        except (OSError, RuntimeError, ValueError, json.JSONDecodeError, subprocess.SubprocessError) as exc:
            report.add("audio_probe", "error", "Could not probe analysis audio: {}".format(exc))

        if video_probe is not None and audio_probe is not None:
            video_duration = _stream_duration_seconds(video_probe, "video")
            audio_duration = _stream_duration_seconds(audio_probe, "audio")
            if video_duration is not None and audio_duration is not None:
                delta = abs(video_duration - audio_duration)
                allowed = max(
                    config.duration_tolerance_seconds,
                    video_duration * config.duration_tolerance_ratio,
                )
                report.metrics.update(
                    {
                        "video_duration_seconds": video_duration,
                        "audio_duration_seconds": audio_duration,
                        "duration_delta_seconds": delta,
                        "duration_tolerance_seconds": allowed,
                    }
                )
                report.add(
                    "duration_delta",
                    "pass" if delta <= allowed else "warning",
                    "Audio/video duration difference is within tolerance"
                    if delta <= allowed
                    else "Audio/video duration difference exceeds tolerance",
                    {"delta_seconds": round(delta, 3), "allowed_seconds": round(allowed, 3)},
                )
            else:
                report.add("duration_delta", "warning", "Duration metadata is unavailable")

        try:
            audio_metrics, audio_checks = _analyse_audio(audio, config, ffmpeg_binary)
            report.metrics.update(audio_metrics)
            report.checks.extend(audio_checks)
        except (OSError, subprocess.SubprocessError) as exc:
            report.add("audio_analysis", "error", "Could not run audio analysis: {}".format(exc))

    if segments_path is None:
        report.add("segments", "skipped", "No segment manifest/SRT was supplied")
    else:
        segments = Path(segments_path)
        if not segments.is_file():
            report.add("segments", "error", "Segment manifest/SRT is missing")
        else:
            try:
                segment_metrics, segment_checks = _check_segments(segments)
                report.metrics.update(segment_metrics)
                report.checks.extend(segment_checks)
            except (OSError, ValueError, json.JSONDecodeError) as exc:
                report.add("segments", "error", "Could not inspect segments: {}".format(exc))

    if ass_path is None:
        report.add("subtitle_safe_area", "skipped", "No ASS subtitle file was supplied")
    else:
        subtitles = Path(ass_path)
        if not subtitles.is_file():
            report.add("subtitle_safe_area", "error", "ASS subtitle file is missing")
        else:
            try:
                subtitle_metrics, subtitle_checks = _check_ass_safe_area(subtitles, config)
                report.metrics["subtitle_safe_area"] = subtitle_metrics
                report.checks.extend(subtitle_checks)
            except OSError as exc:
                report.add("subtitle_safe_area", "error", "Could not inspect ASS file: {}".format(exc))

    video_duration = _duration_seconds(video_probe) if video_probe is not None else None
    if not config.sample_frames:
        report.add("frame_samples", "skipped", "Frame sampling is disabled")
    elif not video.is_file() or video_duration is None or video_duration <= 0:
        report.add("frame_samples", "skipped", "Video duration is unavailable for frame sampling")
    else:
        diagnostics = Path(diagnostics_directory or (Path(report_path).parent / "qc_diagnostics"))
        try:
            frame_artifacts, frame_checks = _sample_frames(
                video,
                video_duration,
                diagnostics,
                ffmpeg_binary,
                config.command_timeout_seconds,
            )
            report.diagnostic_artifacts.extend(frame_artifacts)
            report.checks.extend(frame_checks)
        except (OSError, subprocess.SubprocessError) as exc:
            report.add("frame_samples", "warning", "Could not sample frames: {}".format(exc))

    report.finalize()
    atomic_write_json(report_path, report.to_dict())
    return report


def build_argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Create a non-blocking pipeline v2 QC report")
    parser.add_argument("--video", required=True, help="Final rendered video")
    parser.add_argument("--report", required=True, help="Output qc_report.json")
    parser.add_argument("--audio", help="Optional mixed audio to compare/analyse")
    parser.add_argument("--segments", help="Optional segment JSON or SRT")
    parser.add_argument("--ass", help="Optional ASS subtitle file")
    parser.add_argument("--diagnostics-dir", help="Directory for first/middle/last frames")
    parser.add_argument("--no-frame-samples", action="store_true")
    parser.add_argument("--ffmpeg", default="ffmpeg")
    parser.add_argument("--ffprobe", default="ffprobe")
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_argument_parser().parse_args(argv)
    settings = QCSettings(sample_frames=not args.no_frame_samples)
    report = run_report_only_qc(
        video_path=args.video,
        report_path=args.report,
        audio_path=args.audio,
        segments_path=args.segments,
        ass_path=args.ass,
        diagnostics_directory=args.diagnostics_dir,
        settings=settings,
        ffmpeg_binary=args.ffmpeg,
        ffprobe_binary=args.ffprobe,
    )
    print(json.dumps(report.to_dict()["summary"], ensure_ascii=False))
    # Findings never fail a render or delivery in phase 2.
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
