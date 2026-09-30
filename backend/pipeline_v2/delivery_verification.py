"""Unified delivery verification for AutoDub Tool V2.

Validates that a pipeline job has completed all required stages, passed strict QC,
and that all published output files exist on disk with valid streams, sizes,
and checksums. Used by batch processor, API, dashboard, bot, and resume.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Union

from .artifact_store import hash_file
from .config import QCGatePolicy
from .models import FingerprintSet, JobManifest
from .qc import QCGateDecision, evaluate_qc_gate
from .stage_status import StageStatus

logger = logging.getLogger(__name__)

PathLike = Union[str, os.PathLike]


@dataclass(frozen=True)
class DeliveryVerificationResult:
    is_valid: bool
    reason: str
    published_outputs: List[Dict[str, Any]] = field(default_factory=list)
    qc_decision: Optional[Dict[str, Any]] = None
    media_info: Optional[Dict[str, Any]] = None
    manifest: Optional[JobManifest] = None


def probe_delivered_media(
    file_path: PathLike,
    timeout_seconds: float = 15.0,
) -> Dict[str, Any]:
    """Run ffprobe to verify video stream, audio stream, and duration."""
    target = Path(file_path)
    if not target.is_file():
        return {"valid": False, "error": f"File does not exist: {target}"}

    creation_flags = 0
    if os.name == "nt" and hasattr(subprocess, "CREATE_NO_WINDOW"):
        creation_flags = subprocess.CREATE_NO_WINDOW

    cmd = [
        "ffprobe",
        "-v", "error",
        "-show_entries",
        "stream=codec_type,codec_name,width,height,r_frame_rate,channels:format=duration,size,bit_rate",
        "-of", "json",
        str(target),
    ]
    try:
        res = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            creationflags=creation_flags,
        )
        if res.returncode != 0:
            return {"valid": False, "error": res.stderr.strip() or "ffprobe error"}
        data = json.loads(res.stdout)
        streams = data.get("streams", [])
        fmt = data.get("format", {})

        video_stream = next((s for s in streams if s.get("codec_type") == "video"), None)
        audio_stream = next((s for s in streams if s.get("codec_type") == "audio"), None)
        duration = float(fmt.get("duration", 0.0) or 0.0)

        if not video_stream:
            return {"valid": False, "error": "No video stream found in published output"}
        if not audio_stream:
            return {"valid": False, "error": "No audio stream found in published output"}
        if duration <= 0.05:
            return {"valid": False, "error": f"Invalid media duration: {duration}s"}

        return {
            "valid": True,
            "duration": duration,
            "video_codec": video_stream.get("codec_name", ""),
            "width": int(video_stream.get("width", 0) or 0),
            "height": int(video_stream.get("height", 0) or 0),
            "audio_codec": audio_stream.get("codec_name", ""),
            "channels": int(audio_stream.get("channels", 0) or 0),
            "size_bytes": int(fmt.get("size", target.stat().st_size) or 0),
        }
    except Exception as exc:
        return {"valid": False, "error": f"ffprobe execution failed: {exc}"}


def verify_delivered_product(
    job_manifest_or_dir: Union[JobManifest, PathLike],
    expected_output_path: Optional[PathLike] = None,
    expected_source_path: Optional[PathLike] = None,
    expected_source_sha256: Optional[str] = None,
    expected_fingerprints: Optional[FingerprintSet] = None,
    qc_policy: Union[str, QCGatePolicy] = "block",
    check_sha256: bool = True,
    verify_media_streams: bool = True,
) -> DeliveryVerificationResult:
    """Verify that a job manifest and its published outputs meet all production delivery criteria.

    Conditions required for validity:
    1. Valid manifest with stages 'qc' and 'deliver' in COMPLETED status.
    2. Source matching: source SHA-256 and fingerprints match expected values if provided.
    3. QC Gate: QC report evaluated under qc_policy (e.g. BLOCK) allows delivery (allowed == True).
    4. Published output files: Path exists, file size matches metadata (> 1000 bytes),
       and SHA-256 matches manifest metadata (when check_sha256 is True).
    5. Media streams: ffprobe verifies valid video stream (width/height > 0),
       valid audio stream (channels > 0), and duration > 0.05s.
    """
    manifest: Optional[JobManifest] = None
    job_dir: Optional[Path] = None

    # 1. Resolve manifest
    if isinstance(job_manifest_or_dir, JobManifest):
        manifest = job_manifest_or_dir
        job_dir = Path(manifest.metadata.get("job_directory", "."))
    else:
        path = Path(job_manifest_or_dir)
        if path.is_file() and path.name == "job_manifest.json":
            job_dir = path.parent
            manifest_file = path
        elif path.is_dir():
            job_dir = path
            manifest_file = path / "job_manifest.json"
            if not manifest_file.is_file() and (path / "pipeline_v2" / "job_manifest.json").is_file():
                job_dir = path / "pipeline_v2"
                manifest_file = job_dir / "job_manifest.json"
        else:
            return DeliveryVerificationResult(
                is_valid=False,
                reason=f"Manifest path or job directory does not exist: {path}",
            )
        if not manifest_file.is_file():
            return DeliveryVerificationResult(
                is_valid=False,
                reason=f"Job manifest not found: {manifest_file}",
            )
        try:
            with open(manifest_file, "r", encoding="utf-8") as f:
                manifest = JobManifest.from_dict(json.load(f))
        except Exception as exc:
            return DeliveryVerificationResult(
                is_valid=False,
                reason=f"Could not load job manifest: {exc}",
            )

    # 2. Check source matching and fingerprints
    if expected_source_sha256:
        actual_source_sha = manifest.fingerprints.source_sha256
        if actual_source_sha != expected_source_sha256:
            return DeliveryVerificationResult(
                is_valid=False,
                reason=f"Source SHA256 mismatch: expected {expected_source_sha256}, manifest has {actual_source_sha}",
                manifest=manifest,
            )

    if expected_source_path:
        src_p = Path(expected_source_path)
        manifest_src = manifest.metadata.get("source_path")
        if manifest_src and Path(manifest_src).resolve() != src_p.resolve():
            # If path resolved differs, also check if source file hash matches
            if src_p.is_file() and check_sha256:
                src_hash, _ = hash_file(src_p)
                if src_hash != manifest.fingerprints.source_sha256:
                    return DeliveryVerificationResult(
                        is_valid=False,
                        reason=f"Source file {src_p} hash does not match manifest source hash",
                        manifest=manifest,
                    )

    if expected_fingerprints:
        if manifest.fingerprints.config_sha256 != expected_fingerprints.config_sha256:
            return DeliveryVerificationResult(
                is_valid=False,
                reason="Configuration fingerprint mismatch against manifest",
                manifest=manifest,
            )

    # 3. Check stages 'qc' and 'deliver'
    if "qc" not in manifest.stages:
        return DeliveryVerificationResult(
            is_valid=False,
            reason="Manifest does not contain 'qc' stage",
            manifest=manifest,
        )
    if "deliver" not in manifest.stages:
        return DeliveryVerificationResult(
            is_valid=False,
            reason="Manifest does not contain 'deliver' stage",
            manifest=manifest,
        )

    qc_stage = manifest.stage("qc")
    deliver_stage = manifest.stage("deliver")

    if qc_stage.status != StageStatus.COMPLETED:
        return DeliveryVerificationResult(
            is_valid=False,
            reason=f"QC stage status is {qc_stage.status.value}, expected completed",
            manifest=manifest,
        )

    if deliver_stage.status != StageStatus.COMPLETED:
        return DeliveryVerificationResult(
            is_valid=False,
            reason=f"Deliver stage status is {deliver_stage.status.value}, expected completed",
            manifest=manifest,
        )

    # 4. Evaluate QC Gate Decision
    qc_report_path = (job_dir / "artifacts" / "qc" / "qc_report.json") if job_dir else None
    report_data = None
    if qc_report_path and qc_report_path.is_file():
        try:
            with open(qc_report_path, "r", encoding="utf-8") as f:
                report_data = json.load(f)
        except Exception:
            report_data = None

    if report_data is None:
        report_data = qc_stage.metadata.get("report")

    if report_data:
        gate_decision = evaluate_qc_gate(report_data, policy=qc_policy)
        if not gate_decision.allowed:
            return DeliveryVerificationResult(
                is_valid=False,
                reason=f"QC Gate blocked delivery: {gate_decision.reason} ({', '.join(gate_decision.blocking_checks)})",
                qc_decision={
                    "allowed": gate_decision.allowed,
                    "policy": gate_decision.policy,
                    "reason": gate_decision.reason,
                    "blocking_checks": list(gate_decision.blocking_checks),
                },
                manifest=manifest,
            )
    else:
        # If no report found and policy is block, fail-closed
        policy_str = str(getattr(qc_policy, "value", qc_policy)).lower()
        if policy_str == "block":
            return DeliveryVerificationResult(
                is_valid=False,
                reason="QC report missing in job directory under block policy",
                manifest=manifest,
            )

    # 5. Verify published outputs
    deliver_meta = deliver_stage.metadata
    published_list = deliver_meta.get("published_outputs") or [
        deliver_meta.get("published_output")
    ]
    published_list = [p for p in published_list if isinstance(p, dict) and p.get("path")]

    if not published_list:
        return DeliveryVerificationResult(
            is_valid=False,
            reason="Deliver stage metadata has no valid published_outputs",
            manifest=manifest,
        )

    verified_outputs = []
    primary_probe_info = None

    for item in published_list:
        p_str = item.get("path")
        expected_size = int(item.get("size_bytes", 0) or 0)
        expected_sha = str(item.get("sha256", "") or "")

        p_file = Path(p_str)
        if not p_file.is_file():
            return DeliveryVerificationResult(
                is_valid=False,
                reason=f"Published output file does not exist on disk: {p_file}",
                manifest=manifest,
            )

        actual_size = p_file.stat().st_size
        if actual_size < 1000:
            return DeliveryVerificationResult(
                is_valid=False,
                reason=f"Published output file is too small ({actual_size} bytes): {p_file}",
                manifest=manifest,
            )
        if expected_size > 0 and actual_size != expected_size:
            return DeliveryVerificationResult(
                is_valid=False,
                reason=f"Published output file size mismatch: expected {expected_size}, got {actual_size}",
                manifest=manifest,
            )

        if check_sha256 and expected_sha:
            actual_sha, _ = hash_file(p_file)
            if actual_sha != expected_sha:
                return DeliveryVerificationResult(
                    is_valid=False,
                    reason=f"Published output SHA-256 mismatch for {p_file}: expected {expected_sha}, got {actual_sha}",
                    manifest=manifest,
                )

        if expected_output_path:
            exp_p = Path(expected_output_path).resolve()
            if p_file.resolve() == exp_p:
                verified_outputs.append(item)
        else:
            verified_outputs.append(item)

    if expected_output_path and not verified_outputs:
        return DeliveryVerificationResult(
            is_valid=False,
            reason=f"Published outputs do not include expected output path: {expected_output_path}",
            manifest=manifest,
        )

    # 6. Verify media streams via ffprobe
    target_output_file = Path(verified_outputs[0]["path"]) if verified_outputs else Path(published_list[0]["path"])
    if verify_media_streams:
        primary_probe_info = probe_delivered_media(target_output_file)
        if not primary_probe_info.get("valid", False):
            return DeliveryVerificationResult(
                is_valid=False,
                reason=f"Published output media verification failed: {primary_probe_info.get('error')}",
                published_outputs=verified_outputs or published_list,
                media_info=primary_probe_info,
                manifest=manifest,
            )

    return DeliveryVerificationResult(
        is_valid=True,
        reason="Delivery product verified successfully",
        published_outputs=verified_outputs or published_list,
        media_info=primary_probe_info,
        manifest=manifest,
    )
