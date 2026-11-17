"""
cloud_orchestrator.py - Bộ điều phối thực thi công việc Cloud GPU cho Tool V1.
Quản lý chu kỳ sống có lease, hủy thực sự và thu hồi session theo job/attempt:
  provisioning -> uploading -> preparing -> processing -> downloading -> validating -> completed
  (hoặc cancel_requested -> cancelling -> cancelled khi đã xác nhận máy ảo dừng).
"""

from __future__ import annotations

import errno
import hashlib
import inspect
import io
import json
import logging
import math
import os
import re
import shutil
import tarfile
import tempfile
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from .cloud_config import DEFAULT_CONFIG
from .cloud_contract import (
    CloudCostMeasurementSource,
    CloudErrorCode,
    GPU_CU_RATES,
    build_input_fingerprint,
    probe_video_metadata,
)
from .cloud_job_store import CloudJob, CloudJobStore
from .cloud_security import (
    CloudValidationError,
    redact_secrets,
    sanitize_remote_filename,
    validate_gpu_type,
    validate_session_name,
    validate_task_type,
    validate_video_input,
    validate_voice,
)
from .colab_bridge import ColabProviderAdapter

logger = logging.getLogger("cloud.orchestrator")

_LEASE_LOST_REASONS = frozenset({"lease_lost", "lease_expired", "attempt_mismatch", "owner_mismatch", "not_found"})
_VALID_ERROR_CODES = frozenset(code.value for code in CloudErrorCode)
_SETUP_RESULT_PREFIX = "SETUP_ENV_RESULT_JSON:"
_PIPELINE_RESULT_PREFIX = "PIPELINE_RESULT_JSON:"
_DOWNLOADABLE_REMOTE_ARTIFACTS = frozenset(
    {"output_video.mp4", "result_metadata.json", "subtitles.srt", "subtitles_vi.srt", "subtitles_vi.ass",
     "asr_timing.json", "subtitles.ocr.json", "caption_review.json", "tts_timing.json", "subtitle_timing.json", "pipeline_segments.json"}
)
_V1_CORE_MODULE_NAMES = (
    "ass_utils.py",
    "subtitle_layout.py",
    "subtitle_text.py",
    "v1_ocr_geometry.py",
    "ocr_subtitle_locator.py",
    "v1_timing_solver.py",
    "v1_speech_guard.py",
)


def _file_sha256(path_obj: Path) -> str:
    h = hashlib.sha256()
    with open(path_obj, "rb") as f:
        while True:
            chunk = f.read(1024 * 1024)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def _parse_setup_env_result(output_text: str) -> Optional[Dict[str, Any]]:
    for line in reversed((output_text or "").splitlines()):
        stripped = line.strip()
        idx = stripped.find(_SETUP_RESULT_PREFIX)
        if idx >= 0:
            raw_json = stripped[idx + len(_SETUP_RESULT_PREFIX) :].strip()
            try:
                parsed = json.loads(raw_json)
                if isinstance(parsed, dict):
                    return parsed
            except json.JSONDecodeError:
                continue
    return None


def _extract_setup_error_code(output_text: str) -> str:
    parsed = _parse_setup_env_result(output_text)
    if parsed:
        code = str(parsed.get("error_code") or "").strip()
        if code in _VALID_ERROR_CODES:
            return code
    lowered = (output_text or "").lower()
    if any(
        tok in lowered
        for tok in (
            "newconnectionerror",
            "nameresolutionerror",
            "temporary failure in name resolution",
            "timed out",
            "readtimeouterror",
            "connectionerror",
            "connection was lost",
        )
    ):
        return CloudErrorCode.ENV_NETWORK_ERROR.value
    if "gpu" in lowered and ("mismatch" in lowered or "cuda" in lowered or "nvidia-smi" in lowered):
        return CloudErrorCode.ENV_GPU_MISMATCH.value
    if "disk" in lowered and ("insufficient" in lowered or "space" in lowered):
        return CloudErrorCode.ENV_DISK_INSUFFICIENT.value
    if "model" in lowered and ("load" in lowered or "checksum" in lowered):
        return CloudErrorCode.ENV_MODEL_LOAD_FAILED.value
    if "upload" in lowered or "bundle" in lowered:
        return CloudErrorCode.UPLOAD_VERIFICATION_FAILED.value
    return CloudErrorCode.ENV_SETUP_FAILED.value


def _parse_json_prefix_line(output_text: str, prefix: str) -> Optional[Dict[str, Any]]:
    for line in reversed((output_text or "").splitlines()):
        stripped = line.strip()
        idx = stripped.find(prefix)
        if idx >= 0:
            raw_json = stripped[idx + len(prefix) :].strip()
            try:
                parsed = json.loads(raw_json)
                if isinstance(parsed, dict):
                    return parsed
            except json.JSONDecodeError:
                continue
    return None


def _parse_pipeline_result(output_text: str) -> Optional[Dict[str, Any]]:
    return _parse_json_prefix_line(output_text, _PIPELINE_RESULT_PREFIX)


def _extract_pipeline_error_code(output_text: str) -> str:
    parsed = _parse_pipeline_result(output_text)
    if parsed:
        code = str(parsed.get("error_code") or "").strip()
        if code in _VALID_ERROR_CODES:
            return code
    return CloudErrorCode.EXEC_FAILED.value


class _OrchestratorStageError(RuntimeError):
    """Internal runtime error preserving structured CloudErrorCode across cleanup/transition."""

    def __init__(self, error_code: str, message: str):
        super().__init__(message)
        self.error_code = error_code


class _JobAborted(Exception):
    """Internal control-flow exception raised when a job is cancelled, exceeds budget, or loses its lease."""

    def __init__(self, reason: str, *, is_lease_lost: bool = False, is_budget_exceeded: bool = False):
        super().__init__(reason)
        self.reason = reason
        self.is_lease_lost = is_lease_lost
        self.is_budget_exceeded = is_budget_exceeded


def _supports_kwarg(fn: Any, kwarg_name: str) -> bool:
    try:
        sig = inspect.signature(fn)
    except (TypeError, ValueError):
        return False
    for param in sig.parameters.values():
        if param.kind == inspect.Parameter.VAR_KEYWORD or param.name == kwarg_name:
            return True
    return False


def _call_with_cancel(fn: Any, *args: Any, cancel_check: Optional[Callable[[], bool]] = None, **kwargs: Any) -> Any:
    if cancel_check is not None and _supports_kwarg(fn, "cancel_check"):
        kwargs["cancel_check"] = cancel_check
    return fn(*args, **kwargs)


class CloudOrchestrator:
    def __init__(
        self,
        store: Optional[CloudJobStore] = None,
        adapter: Optional[ColabProviderAdapter] = None,
        allowed_roots: Optional[Sequence[str | Path]] = None,
    ):
        self.store = store or CloudJobStore()
        self.adapter = adapter or ColabProviderAdapter()
        self.allowed_roots = allowed_roots
        self.backend_dir = Path(__file__).resolve().parent.parent
        self.gpu_scripts_dir = Path(__file__).resolve().parent / "gpu_scripts"

    def _verify_or_stop_session(self, session_name: str, extra_secrets: Sequence[str] = ()) -> Dict[str, Any]:
        verify_fn = getattr(self.adapter, "verify_session_stopped", None)
        if callable(verify_fn):
            try:
                res = verify_fn(session_name)
                if isinstance(res, dict) and "verified_stopped" in res:
                    cleaned = dict(res)
                    if cleaned.get("error"):
                        cleaned["error"] = redact_secrets(
                            str(cleaned["error"]), extra_secrets=extra_secrets
                        )
                    return cleaned
            except Exception as exc:
                safe_err = redact_secrets(str(exc), extra_secrets=extra_secrets)
                return {
                    "verified_stopped": False,
                    "cleanup_status": "failed_retry_needed",
                    "attempts": 1,
                    "error": safe_err,
                }

        try:
            stop_ok, stop_msg = self.adapter.stop_session(session_name)
        except Exception as exc:
            safe_err = redact_secrets(str(exc), extra_secrets=extra_secrets)
            return {
                "verified_stopped": False,
                "cleanup_status": "failed_retry_needed",
                "attempts": 1,
                "error": safe_err,
            }

        status_fn = getattr(self.adapter, "get_sessions_status", None)
        if callable(status_fn):
            try:
                st = status_fn()
                if not st.get("ok"):
                    return {
                        "verified_stopped": False,
                        "cleanup_status": "provider_unreachable",
                        "attempts": 1,
                        "error": redact_secrets(str(st.get("error") or stop_msg), extra_secrets=extra_secrets),
                    }
                still_present = any(
                    str(item.get("name")) == session_name for item in (st.get("sessions") or [])
                )
                if stop_ok and not still_present:
                    return {
                        "verified_stopped": True,
                        "cleanup_status": "verified",
                        "attempts": 1,
                        "error": None,
                    }
                return {
                    "verified_stopped": False,
                    "cleanup_status": "failed_retry_needed",
                    "attempts": 1,
                    "error": redact_secrets(
                        f"Session {session_name} vẫn tồn tại sau lệnh dừng." if still_present else str(stop_msg),
                        extra_secrets=extra_secrets,
                    ),
                }
            except Exception as exc:
                return {
                    "verified_stopped": False,
                    "cleanup_status": "provider_unreachable",
                    "attempts": 1,
                    "error": redact_secrets(str(exc), extra_secrets=extra_secrets),
                }

        return {
            "verified_stopped": bool(stop_ok),
            "cleanup_status": "verified" if stop_ok else "failed_retry_needed",
            "attempts": 1,
            "error": None if stop_ok else redact_secrets(str(stop_msg), extra_secrets=extra_secrets),
        }

    def _resolve_gpu_scripts_dir(self) -> Path:
        if self.gpu_scripts_dir.is_dir():
            return self.gpu_scripts_dir
        alt = Path(__file__).resolve().parent / "gpu_scripts"
        if alt.is_dir():
            return alt
        return self.gpu_scripts_dir

    def verify_local_preflight(
        self,
        validated_input: Any,
        *,
        max_upload_size_bytes: Optional[int] = None,
    ) -> Dict[str, Any]:
        """
        Kiểm tra toàn bộ điều kiện đầu vào và bundle cục bộ trước khi cấp phát GPU (0 CU):
        - Manifest môi trường, requirements_cloud.txt và checksum .whl
        - Đầy đủ module lõi V1 và gpu_scripts
        - Dung lượng & checksum file video local (không rỗng, không vượt ngưỡng)
        """
        scripts_dir = self._resolve_gpu_scripts_dir()
        manifest_path = scripts_dir / "cloud_env_manifest.json"
        if not manifest_path.is_file():
            raise _OrchestratorStageError(
                CloudErrorCode.ENV_SETUP_FAILED.value,
                f"Thiếu cloud_env_manifest.json tại {manifest_path}",
            )
        try:
            env_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise _OrchestratorStageError(
                CloudErrorCode.ENV_SETUP_FAILED.value,
                f"Không đọc được cloud_env_manifest.json: {exc}",
            ) from exc

        req_path = scripts_dir / str(env_manifest.get("requirements_file", "requirements_cloud.txt"))
        if not req_path.is_file() or req_path.stat().st_size == 0:
            raise _OrchestratorStageError(
                CloudErrorCode.ENV_PACKAGE_MISSING.value,
                f"Thiếu hoặc rỗng file requirements Cloud: {req_path.name}",
            )

        for wheel_spec in env_manifest.get("wheels", []):
            wheel_name = wheel_spec.get("filename", "")
            wheel_path = scripts_dir / wheel_name
            if not wheel_path.is_file():
                raise _OrchestratorStageError(
                    CloudErrorCode.ENV_PACKAGE_MISSING.value,
                    f"Thiếu gói wheel bắt buộc: {wheel_name}",
                )
            expected_sha = str(wheel_spec.get("sha256") or "").strip()
            if expected_sha:
                actual_sha = _file_sha256(wheel_path)
                if actual_sha != expected_sha:
                    raise _OrchestratorStageError(
                        CloudErrorCode.ENV_PACKAGE_MISSING.value,
                        f"Sai checksum SHA256 cho wheel {wheel_name} (cục bộ bị hỏng hoặc chưa đồng bộ).",
                    )

        backend_root = scripts_dir.parent.parent
        v1_core_paths = [backend_root / name for name in _V1_CORE_MODULE_NAMES]
        missing_modules: List[str] = []
        for mod_name in env_manifest.get("required_v1_modules", []):
            candidate_scripts = scripts_dir / mod_name
            candidate_backend = backend_root / mod_name
            target = candidate_backend if mod_name in _V1_CORE_MODULE_NAMES else candidate_scripts
            if not target.is_file() or target.stat().st_size == 0:
                missing_modules.append(mod_name)
        if missing_modules:
            raise _OrchestratorStageError(
                CloudErrorCode.ENV_PACKAGE_MISSING.value,
                f"Thiếu module bắt buộc trước khi đóng gói lên Cloud: {', '.join(missing_modules)}",
            )

        all_files = (
            list(scripts_dir.glob("*.whl"))
            + list(scripts_dir.glob("*.py"))
            + [req_path, manifest_path]
            + v1_core_paths
        )
        upload_targets = list({p.name: p for p in all_files if p.is_file()}.values())

        source_snapshot: Optional[Dict[str, Any]] = None
        if getattr(validated_input, "input_kind", "local_file") == "local_file":
            src_path = Path(validated_input.normalized_value)
            if not src_path.is_file():
                raise _OrchestratorStageError(
                    CloudErrorCode.INVALID_INPUT.value,
                    f"Không tìm thấy file video đầu vào: {src_path.name}",
                )
            size_bytes = src_path.stat().st_size
            if size_bytes <= 0:
                raise _OrchestratorStageError(
                    CloudErrorCode.INVALID_INPUT.value,
                    f"File video đầu vào rỗng (0 byte): {src_path.name}",
                )
            limit_bytes = int(
                max_upload_size_bytes
                if max_upload_size_bytes is not None
                else DEFAULT_CONFIG.get("max_upload_size_bytes", 2 * 1024 * 1024 * 1024)
            )
            if limit_bytes > 0 and size_bytes > limit_bytes:
                raise _OrchestratorStageError(
                    CloudErrorCode.INVALID_INPUT.value,
                    f"File video {src_path.name} ({size_bytes} bytes) vượt giới hạn tải lên ({limit_bytes} bytes).",
                )
            fingerprint = build_input_fingerprint(str(src_path), kind="local_file")
            source_snapshot = {
                "path": str(src_path),
                "size_bytes": size_bytes,
                "sha256": fingerprint["sha256"],
            }
        else:
            fingerprint = build_input_fingerprint(validated_input.normalized_value, kind="url")

        return {
            "env_manifest": env_manifest,
            "upload_targets": upload_targets,
            "fingerprint": fingerprint,
            "source_snapshot": source_snapshot,
        }

    def validate_downloaded_output(
        self,
        dl_file: Path,
        attempt_res_dir: Path,
        *,
        source_fingerprint: Optional[Dict[str, Any]] = None,
        extra_secrets: Sequence[str] = (),
    ) -> Dict[str, Any]:
        """
        Kiểm tra toàn diện video & metadata tải về tại vùng staging theo attempt trước khi công bố.
        Nếu QC fail, lưu qc_report.json để chẩn đoán trong attempt_res_dir và chặn xuất ra final_out_dir.
        """
        qc_report: Dict[str, Any] = {
            "passed": False,
            "staging_dir": str(attempt_res_dir),
            "downloaded_file": str(dl_file),
            "checks": [],
            "error_code": None,
            "error": None,
        }

        def _persist_qc_and_fail(err_code: str, message: str) -> None:
            safe_msg = redact_secrets(message, extra_secrets=extra_secrets)
            qc_report["passed"] = False
            qc_report["error_code"] = err_code
            qc_report["error"] = safe_msg
            try:
                (attempt_res_dir / "qc_report.json").write_text(
                    redact_secrets(json.dumps(qc_report, indent=2, ensure_ascii=False), extra_secrets=extra_secrets),
                    encoding="utf-8",
                )
            except OSError:
                pass
            raise _OrchestratorStageError(err_code, safe_msg)

        if not dl_file.is_file():
            _persist_qc_and_fail(CloudErrorCode.DOWNLOAD_FAILED.value, "File video tải về không tồn tại trên đĩa.")
        dl_size = dl_file.stat().st_size
        if dl_size <= 0:
            _persist_qc_and_fail(CloudErrorCode.DOWNLOAD_FAILED.value, "File video tải về bị rỗng (0 byte).")

        dl_sha = _file_sha256(dl_file)
        qc_report["size_bytes"] = dl_size
        qc_report["sha256"] = dl_sha
        qc_report["checks"].append("download_non_empty")

        remote_meta: Dict[str, Any] = {}
        meta_file = attempt_res_dir / "result_metadata.json"
        if meta_file.is_file():
            try:
                raw_meta_text = redact_secrets(
                    meta_file.read_text(encoding="utf-8"),
                    extra_secrets=extra_secrets,
                )
                meta_file.write_text(raw_meta_text, encoding="utf-8")
                parsed_meta = json.loads(raw_meta_text or "{}")
                if isinstance(parsed_meta, dict):
                    remote_meta = parsed_meta
            except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
                _persist_qc_and_fail(
                    CloudErrorCode.QC_FAILED.value,
                    f"File result_metadata.json không hợp lệ: {exc}",
                )

        if remote_meta:
            r_status = remote_meta.get("status")
            if r_status not in (None, "completed"):
                r_code = str(remote_meta.get("error_code") or "").strip()
                err_code = r_code if r_code in _VALID_ERROR_CODES else CloudErrorCode.QC_FAILED.value
                _persist_qc_and_fail(
                    err_code,
                    f"Metadata Colab báo trạng thái lỗi ({r_status}): {remote_meta.get('error') or 'unknown'}",
                )

            if "segments_count" in remote_meta:
                seg_count = int(remote_meta.get("segments_count") or 0)
                no_speech_mode = bool(remote_meta.get("no_speech_mode", False))
                if seg_count <= 0 and not no_speech_mode:
                    _persist_qc_and_fail(
                        CloudErrorCode.NO_SPEECH_DETECTED.value,
                        "Metadata báo 0 câu thoại và không bật no_speech_mode.",
                    )
                translated_count = int(remote_meta.get("translated_count", seg_count))
                if translated_count != seg_count:
                    _persist_qc_and_fail(
                        CloudErrorCode.TRANSLATION_FAILED.value,
                        f"Số câu đã dịch không khớp ASR ({translated_count}/{seg_count}).",
                    )
                tts_count = int(remote_meta.get("tts_verified_count", seg_count))
                if not no_speech_mode and tts_count != seg_count:
                    _persist_qc_and_fail(
                        CloudErrorCode.TTS_GENERATION_FAILED.value,
                        f"Số file TTS đạt chuẩn không khớp số câu ({tts_count}/{seg_count}).",
                    )
                qc_report["checks"].append("translation_and_tts_counts")

            artifacts = remote_meta.get("artifacts") or {}
            video_art = artifacts.get("output_video.mp4") if isinstance(artifacts, dict) else None
            if isinstance(video_art, dict):
                exp_size = video_art.get("size_bytes")
                exp_sha = str(video_art.get("sha256") or "").strip()
                if exp_size is not None and int(exp_size) != dl_size:
                    _persist_qc_and_fail(
                        CloudErrorCode.DOWNLOAD_FAILED.value,
                        f"File tải về bị cắt cụt/sai dung lượng ({dl_size} != {exp_size} bytes).",
                    )
                if exp_sha and exp_sha != dl_sha:
                    _persist_qc_and_fail(
                        CloudErrorCode.DOWNLOAD_FAILED.value,
                        f"Checksum SHA256 file tải về không khớp artifact từ Colab.",
                    )
                qc_report["checks"].append("artifact_size_sha256")

            remote_probe = remote_meta.get("output_video_probe")
            if isinstance(remote_probe, dict):
                if remote_probe.get("video_codec") is None or remote_probe.get("audio_codec") is None:
                    _persist_qc_and_fail(
                        CloudErrorCode.QC_FAILED.value,
                        "Probe từ Colab thiếu video_codec hoặc audio_codec.",
                    )
                if remote_probe.get("size_bytes") is not None and int(remote_probe["size_bytes"]) != dl_size:
                    _persist_qc_and_fail(
                        CloudErrorCode.DOWNLOAD_FAILED.value,
                        f"Kích thước tải về ({dl_size}) khác output_video_probe ({remote_probe['size_bytes']}).",
                    )
                if remote_probe.get("sha256") and str(remote_probe["sha256"]) != dl_sha:
                    _persist_qc_and_fail(
                        CloudErrorCode.DOWNLOAD_FAILED.value,
                        "SHA256 tải về không khớp output_video_probe.",
                    )

        local_probe = probe_video_metadata(dl_file)
        qc_report["local_probe"] = local_probe
        if local_probe.get("probe_method") == "ffprobe":
            if not local_probe.get("video_codec"):
                _persist_qc_and_fail(CloudErrorCode.QC_FAILED.value, "QC local: Output thiếu stream hình ảnh.")
            if not local_probe.get("audio_codec"):
                _persist_qc_and_fail(CloudErrorCode.QC_FAILED.value, "QC local: Output thiếu stream âm thanh.")
            dur_s = local_probe.get("duration_seconds")
            if dur_s is None or not math.isfinite(float(dur_s)) or float(dur_s) <= 0.0:
                _persist_qc_and_fail(CloudErrorCode.QC_FAILED.value, f"QC local: Thời lượng output không hợp lệ ({dur_s}).")
            expected_dur = (
                (source_fingerprint or {}).get("duration_seconds")
                or remote_meta.get("input_duration_seconds")
            )
            if expected_dur is not None and float(expected_dur) > 0.0:
                tol = max(1.5, float(expected_dur) * 0.15)
                if abs(float(dur_s) - float(expected_dur)) > tol:
                    _persist_qc_and_fail(
                        CloudErrorCode.QC_FAILED.value,
                        f"QC local: Thời lượng output ({float(dur_s):.2f}s) lệch quá ngưỡng so với nguồn ({float(expected_dur):.2f}s).",
                    )
            qc_report["checks"].append("local_ffprobe_streams_and_duration")
        else:
            if not local_probe.get("header_valid") or dl_size < 24:
                _persist_qc_and_fail(
                    CloudErrorCode.QC_FAILED.value,
                    "QC local: File tải về không có header container video hợp lệ hoặc quá nhỏ.",
                )
            qc_report["checks"].append("local_container_header")

        qc_report["passed"] = True
        try:
            (attempt_res_dir / "qc_report.json").write_text(
                redact_secrets(json.dumps(qc_report, indent=2, ensure_ascii=False), extra_secrets=extra_secrets),
                encoding="utf-8",
            )
        except OSError:
            pass

        return {
            "qc_report": qc_report,
            "local_probe": local_probe,
            "remote_metadata": remote_meta,
            "sha256": dl_sha,
            "size_bytes": dl_size,
        }

    def export_deliverable_atomic(
        self,
        dl_file: Path,
        final_out_dir: Path,
        *,
        job_id: str,
        attempt: int,
        video_name: str,
        expected_sha256: str,
    ) -> Path:
        """
        Xuất file thành phẩm theo tên chứa job_id & attempt (chống ghi đè bản cũ),
        ghi vào file .part trên cùng filesystem rồi os.replace nguyên tử sau khi xác minh checksum.
        """
        final_out_dir.mkdir(parents=True, exist_ok=True)
        raw_base = Path(str(video_name or "video.mp4").replace("\x00", "")).name
        parsed_base = Path(raw_base)
        safe_stem = re.sub(r'[<>:"/\\|?*\x00-\x1f]+', "_", parsed_base.stem).strip(". ") or "video"
        raw_ext = parsed_base.suffix.lower()
        safe_ext = raw_ext if re.fullmatch(r"\.[a-z0-9]{1,8}", raw_ext or "") else ".mp4"
        safe_job = re.sub(r"[^a-zA-Z0-9_-]", "", str(job_id)) or "job"
        safe_attempt = max(1, int(attempt or 1))
        prefix = str(DEFAULT_CONFIG.get("output_prefix") or "cloud_")

        candidate = final_out_dir / f"{prefix}{safe_stem}__{safe_job}_a{safe_attempt}{safe_ext}"
        version_idx = 2
        while candidate.exists():
            candidate = final_out_dir / f"{prefix}{safe_stem}__{safe_job}_a{safe_attempt}_v{version_idx}{safe_ext}"
            version_idx += 1

        tmp_part = final_out_dir / f".{candidate.name}.{uuid.uuid4().hex[:8]}.part"
        expected_size = dl_file.stat().st_size
        try:
            with open(dl_file, "rb") as src, open(tmp_part, "wb") as dst:
                while True:
                    buf = src.read(1024 * 1024)
                    if not buf:
                        break
                    dst.write(buf)
                dst.flush()
                os.fsync(dst.fileno())

            if not tmp_part.is_file() or tmp_part.stat().st_size != expected_size:
                raise _OrchestratorStageError(
                    CloudErrorCode.EXPORT_FAILED.value,
                    "Kích thước file tạm .part không khớp sau khi sao chép.",
                )
            if expected_sha256 and _file_sha256(tmp_part) != expected_sha256:
                raise _OrchestratorStageError(
                    CloudErrorCode.EXPORT_FAILED.value,
                    "Checksum SHA256 file tạm .part bị sai lệch khi xuất thành phẩm.",
                )
            os.replace(tmp_part, candidate)
        except OSError as exc:
            err_code = (
                CloudErrorCode.ENV_DISK_INSUFFICIENT.value
                if getattr(exc, "errno", None) == errno.ENOSPC
                else CloudErrorCode.EXPORT_FAILED.value
            )
            raise _OrchestratorStageError(err_code, f"Lỗi xuất file thành phẩm an toàn: {exc}") from exc
        finally:
            if tmp_part.exists():
                try:
                    tmp_part.unlink()
                except OSError:
                    pass

        if not candidate.is_file() or candidate.stat().st_size != expected_size:
            raise _OrchestratorStageError(
                CloudErrorCode.EXPORT_FAILED.value,
                "Không xác nhận được file thành phẩm sau os.replace.",
            )
        return candidate

    def process_job(
        self,
        job: CloudJob,
        progress_callback: Optional[Callable[[int, str], None]] = None,
        *,
        owner_id: Optional[str] = None,
        cancel_event: Optional[threading.Event] = None,
    ) -> bool:
        """
        Thực thi trọn vẹn 1 công việc trên Colab GPU theo hợp đồng trạng thái Phase 2.
        Kiểm tra hủy & lease trước/sau mỗi bước chặn và chỉ xác nhận `cancelled`
        sau khi đã xác nhận session của job/attempt dừng thực sự.
        """
        job_id = job.job_id
        raw_attempt = int(getattr(job, "attempt", 0) or 0)
        session_attempt = raw_attempt if raw_attempt > 0 else 1
        effective_owner = owner_id if owner_id is not None else getattr(job, "owner_id", None)
        ownership_attempt = raw_attempt if raw_attempt > 0 else None

        clean_id = re.sub(r"[^a-zA-Z0-9]", "", job_id)[-8:] or uuid.uuid4().hex[:6]
        session_name = validate_session_name(f"cjob{clean_id}a{session_attempt}")
        gemini_key = os.getenv("GEMINI_API_KEY", "").strip()
        extra_secrets = (gemini_key,) if gemini_key else ()
        start_time = time.time()
        runtime_start_time: Optional[float] = None
        last_budget_check_ts = start_time
        budget_abort_reason = ""
        rate_per_hour = GPU_CU_RATES.get("T4", 1.15)
        latest_provider_balance: Optional[float] = None

        session_may_exist = False
        cleanup_done = False
        cleanup_result: Optional[Dict[str, Any]] = None
        final_video_path: Optional[Path] = None

        def _estimate_elapsed_cu() -> float:
            if not session_may_exist or runtime_start_time is None:
                return 0.0
            elapsed_sec = max(0.0, time.time() - runtime_start_time)
            return round((elapsed_sec / 3600.0) * rate_per_hour, 4)

        def _sync_runtime_usage(*, check_provider: bool = False) -> Tuple[bool, str]:
            nonlocal last_budget_check_ts, latest_provider_balance, budget_abort_reason
            if not hasattr(self.store, "record_runtime_usage"):
                return True, "ok"
            est_cu = _estimate_elapsed_cu()
            prov_bal: Optional[float] = None
            if check_provider:
                try:
                    u = self.adapter.get_usage()
                    if hasattr(self.store, "record_provider_usage_snapshot"):
                        snap = self.store.record_provider_usage_snapshot(u)
                        if not snap.get("ok") or snap.get("is_stale"):
                            budget_abort_reason = (
                                "Mất kết nối hoặc số dư Colab bị cũ trong lúc đang chạy session."
                            )
                            return False, "budget_uncertain"
                        prov_bal = snap.get("balance")
                        if prov_bal is not None:
                            latest_provider_balance = float(prov_bal)
                    elif u.get("ok") and u.get("balance") is not None:
                        prov_bal = float(u["balance"])
                        latest_provider_balance = prov_bal
                except Exception as probe_exc:
                    budget_abort_reason = redact_secrets(str(probe_exc), extra_secrets=extra_secrets)
                    return False, "budget_uncertain"

            res = self.store.record_runtime_usage(
                job_id,
                session_attempt,
                est_cu,
                measurement_source=CloudCostMeasurementSource.RUNTIME_RATE_ESTIMATE.value,
                provider_balance=prov_bal,
            )
            if not res.get("within_budget", True):
                budget_abort_reason = (
                    "Đã vượt hạn mức CU trong ngày hoặc hết số dư trong khi đang chạy."
                )
                return False, "budget_exceeded"
            return True, "ok"

        def _check_abort() -> Tuple[bool, str]:
            nonlocal last_budget_check_ts
            if cancel_event is not None and cancel_event.is_set():
                return True, "cancel_event"
            lost, reason = self.store.is_cancel_or_lease_lost(
                job_id,
                owner_id=effective_owner,
                attempt=ownership_attempt,
            )
            if lost:
                return True, reason

            now_ts = time.time()
            interval = max(0.01, float(DEFAULT_CONFIG.get("runtime_budget_check_interval_seconds", 15)))
            if session_may_exist and (now_ts - last_budget_check_ts) >= interval:
                last_budget_check_ts = now_ts
                ok_budget, b_reason = _sync_runtime_usage(check_provider=True)
                if not ok_budget:
                    return True, b_reason
            return False, "ok"

        def _cancel_probe() -> bool:
            aborted, _ = _check_abort()
            return aborted

        def _raise_if_aborted() -> None:
            aborted, reason = _check_abort()
            if aborted:
                is_lost = reason in _LEASE_LOST_REASONS
                is_budget = reason in ("budget_exceeded", "budget_uncertain")
                raise _JobAborted(
                    reason,
                    is_lease_lost=is_lost,
                    is_budget_exceeded=is_budget,
                )

        def _update(percent: int, step_name: str, **kwargs) -> bool:
            safe_step = redact_secrets(step_name, extra_secrets=extra_secrets)
            if "error" in kwargs and kwargs["error"] is not None:
                kwargs["error"] = redact_secrets(str(kwargs["error"]), extra_secrets=extra_secrets)
            if effective_owner is not None and "owner_id" not in kwargs:
                kwargs["owner_id"] = effective_owner
            if ownership_attempt is not None and "attempt" not in kwargs:
                kwargs["attempt"] = ownership_attempt
            ok = self.store.update_progress(job_id, percent=percent, step_name=safe_step, **kwargs)
            if ok and progress_callback:
                try:
                    progress_callback(percent, safe_step)
                except Exception:
                    pass
            return ok

        def _finalize_session_once() -> Dict[str, Any]:
            nonlocal cleanup_done, cleanup_result
            if cleanup_done:
                if cleanup_result is not None:
                    return dict(cleanup_result)
                return {
                    "verified_stopped": True,
                    "cleanup_status": "verified",
                    "attempts": 0,
                    "error": None,
                }
            if not session_may_exist:
                return {
                    "verified_stopped": True,
                    "cleanup_status": "verified",
                    "attempts": 0,
                    "error": None,
                }
            cleanup_done = True
            logger.info("Dọn dẹp: Dừng và xác nhận session Colab %s...", session_name)
            res = self._verify_or_stop_session(session_name, extra_secrets=extra_secrets)
            cleanup_result = dict(res)
            safe_cleanup_err = (
                redact_secrets(str(res.get("error")), extra_secrets=extra_secrets)
                if res.get("error")
                else None
            )
            self.store.record_cleanup_result(
                job_id,
                raw_attempt,
                session_name,
                verified_stopped=bool(res.get("verified_stopped")),
                cleanup_status=str(res.get("cleanup_status") or "failed_retry_needed"),
                attempts=int(res.get("attempts") or 1),
                error=safe_cleanup_err,
            )
            if not res.get("verified_stopped"):
                logger.error(
                    "Cảnh báo: Chưa xác nhận dừng session %s (%s): %s",
                    session_name,
                    res.get("cleanup_status"),
                    safe_cleanup_err,
                )
            return res

        try:
            _raise_if_aborted()

            # 0. Kiểm tra allowlist GPU/task/voice, đầu vào và bundle cục bộ (0 CU) trước khi cấp phát GPU
            preflight_info: Dict[str, Any] = {}
            source_snapshot: Optional[Dict[str, Any]] = None
            try:
                gpu_type = validate_gpu_type(job.gpu_type)
                validate_task_type(getattr(job, "task_type", "full_pipeline"))
                voice_choice = validate_voice(getattr(job, "voice", "BV562_streaming") or "BV562_streaming")
                validated_input = validate_video_input(
                    job.video_path,
                    allowed_roots=self.allowed_roots,
                    require_exists=True,
                )
                preflight_info = self.verify_local_preflight(validated_input)
                source_snapshot = preflight_info.get("source_snapshot")
                rate_per_hour = GPU_CU_RATES.get(gpu_type, 1.15)
            except Exception as val_exc:
                safe_err = redact_secrets(str(val_exc), extra_secrets=extra_secrets)
                logger.warning("Từ chối Cloud Job %s ở bước preflight cục bộ: %s", job_id, safe_err)
                if isinstance(val_exc, CloudValidationError):
                    err_code = val_exc.code
                elif isinstance(val_exc, _OrchestratorStageError):
                    err_code = val_exc.error_code
                else:
                    err_code = CloudErrorCode.INVALID_INPUT.value
                _update(
                    0,
                    f"Lỗi tiền kiểm cục bộ: {safe_err[:100]}",
                    state="failed",
                    error=safe_err,
                    error_code=err_code,
                    cu_actual=0.0,
                )
                return False

            logger.info(
                "=== BẮT ĐẦU XỬ LÝ CLOUD JOB %s (Attempt=%d, Session=%s, Video=%s, GPU=%s) ===",
                job_id,
                session_attempt,
                session_name,
                validated_input.display_name,
                gpu_type,
            )

            _raise_if_aborted()

            # 1. Kiểm tra số dư Compute Units, độ mới snapshot và ngân sách trước khi cấp phát máy
            usage = self.adapter.get_usage()
            if hasattr(self.store, "record_provider_usage_snapshot"):
                snap = self.store.record_provider_usage_snapshot(usage)
            else:
                snap = {
                    "ok": bool(usage.get("ok")),
                    "balance": usage.get("balance"),
                    "is_stale": bool(usage.get("stale", False)),
                    "error": usage.get("error"),
                }

            if not snap.get("ok") or snap.get("is_stale"):
                err = redact_secrets(
                    f"Không thể xác nhận số dư Colab mới ({snap.get('error') or 'stale/disconnected'}). Chặn mở GPU.",
                    extra_secrets=extra_secrets,
                )
                _update(
                    0,
                    "Lỗi: Chưa xác nhận được số dư Colab",
                    state="failed",
                    error=err,
                    error_code=CloudErrorCode.BUDGET_UNCERTAIN.value,
                )
                return False

            balance = snap.get("balance")
            required_bal = max(
                0.5,
                float(getattr(job, "cu_reserved", 0.0) or getattr(job, "cu_estimated", 0.0) or 0.5),
                float(DEFAULT_CONFIG.get("min_provider_balance_cu", 0.0)),
            )
            if balance is not None and float(balance) < required_bal - 1e-6:
                err = f"Số dư Colab không đủ (còn {float(balance):.2f} < {required_bal:.2f} CU). Vui lòng nạp thêm CU."
                _update(
                    0,
                    "Lỗi: Hết Compute Units",
                    state="failed",
                    error=err,
                    error_code=CloudErrorCode.INSUFFICIENT_CU.value,
                )
                return False
            if balance is not None:
                latest_provider_balance = float(balance)

            budget_info = self.store.get_cu_budget_info()
            if int(budget_info.get("unreconciled_count", 0)) > 0:
                err = "Còn phiên GPU trước chưa xác nhận thu hồi/đối soát chi phí; tạm chặn cấp phát mới."
                logger.warning(err)
                _update(
                    0,
                    "Lỗi: Chờ đối soát chi phí phiên trước",
                    state="failed",
                    error=err,
                    error_code=CloudErrorCode.BUDGET_UNCERTAIN.value,
                )
                return False

            budget_daily = float(budget_info["budget_daily"])
            over_daily = (
                budget_daily <= 0.0
                or float(budget_info["spent_today"]) >= budget_daily
                or float(budget_info.get("committed_today", budget_info["spent_today"])) > budget_daily + 1e-6
            )
            if over_daily:
                err = (
                    f"Đã chạm hạn mức CU trong ngày "
                    f"({budget_info.get('committed_today', budget_info['spent_today']):.2f} / {budget_daily:.2f} CU). "
                    f"Vui lòng cập nhật hạn mức trên Dashboard hoặc đợi sang ngày mới để bảo vệ tài khoản!"
                )
                logger.warning(err)
                _update(
                    0,
                    "Lỗi: Đã chạm hạn mức CU hôm nay",
                    state="failed",
                    error=err,
                    error_code=CloudErrorCode.BUDGET_EXCEEDED.value,
                )
                return False

            _raise_if_aborted()

            # 2. Đăng ký ownership session & khởi tạo Session Colab (`provisioning`)
            reg_ok = self.store.register_session(job_id, raw_attempt, effective_owner, session_name)
            if ownership_attempt is not None and not reg_ok:
                _raise_if_aborted()
                raise _JobAborted("attempt_mismatch", is_lease_lost=True)

            if not _update(
                10,
                f"Đang tạo máy ảo GPU {gpu_type} trên Colab...",
                state="provisioning",
                session_name=session_name,
            ):
                _raise_if_aborted()
                raise _JobAborted("lease_lost", is_lease_lost=True)

            session_may_exist = True
            runtime_start_time = time.time()
            created, msg = _call_with_cancel(
                self.adapter.create_session,
                session_name,
                gpu=gpu_type,
                cancel_check=_cancel_probe,
            )

            # Đóng race hủy trong lúc create_session: nếu hủy trong/sau khi tạo thì thu hồi ngay, không upload/chạy pipeline.
            aborted_after_create, abort_reason = _check_abort()
            if aborted_after_create:
                raise _JobAborted(
                    abort_reason,
                    is_lease_lost=(abort_reason in _LEASE_LOST_REASONS),
                    is_budget_exceeded=(abort_reason in ("budget_exceeded", "budget_uncertain")),
                )

            if not created:
                session_may_exist = False
                err_code = (
                    CloudErrorCode.PROVIDER_503.value
                    if "503" in str(msg) or "hết máy" in str(msg).lower()
                    else CloudErrorCode.SESSION_CREATE_FAILED.value
                )
                _update(
                    0,
                    "Lỗi: Tạo máy ảo Colab thất bại",
                    state="failed",
                    error=f"Không thể tạo máy Colab: {msg}",
                    error_code=err_code,
                    cleanup_status="verified",
                )
                self.store.record_cleanup_result(
                    job_id,
                    raw_attempt,
                    session_name,
                    verified_stopped=True,
                    cleanup_status="verified",
                    attempts=0,
                    error=None,
                )
                return False

            # 3. Upload mã nguồn và video lên VM (`uploading` đơn hướng, không lùi từ preparing)
            is_url = validated_input.kind == "url"
            if not _update(
                20,
                "Đang đóng gói và tải mã nguồn AI lên máy ảo Colab...",
                state="uploading",
            ):
                _raise_if_aborted()
                raise _JobAborted("lease_lost", is_lease_lost=True)

            bundle_path = Path(tempfile.gettempdir()) / f"bundle_{session_name}_{uuid.uuid4().hex[:6]}.tar.gz"
            try:
                upload_targets: List[Path] = list(preflight_info.get("upload_targets") or [])
                bundle_manifest_data: Dict[str, Any] = {
                    "schema_version": 1,
                    "job_id": job_id,
                    "attempt": session_attempt,
                    "expected_gpu": gpu_type,
                    "files": [
                        {
                            "name": s_file.name,
                            "size_bytes": s_file.stat().st_size,
                            "sha256": _file_sha256(s_file),
                        }
                        for s_file in upload_targets
                    ],
                }
                if not is_url and source_snapshot:
                    bundle_manifest_data["expected_video"] = {
                        "remote_filename": sanitize_remote_filename(validated_input.display_name),
                        "size_bytes": source_snapshot["size_bytes"],
                        "sha256": source_snapshot["sha256"],
                    }

                bundle_manifest_bytes = json.dumps(bundle_manifest_data, indent=2, ensure_ascii=False).encode("utf-8")
                with tarfile.open(bundle_path, "w:gz") as tar:
                    for s_file in upload_targets:
                        tar.add(s_file, arcname=s_file.name)
                    manifest_tarinfo = tarfile.TarInfo(name="bundle_manifest.json")
                    manifest_tarinfo.size = len(bundle_manifest_bytes)
                    manifest_tarinfo.mtime = int(time.time())
                    tar.addfile(manifest_tarinfo, io.BytesIO(bundle_manifest_bytes))

                _raise_if_aborted()
                up_ok, up_msg = _call_with_cancel(
                    self.adapter.upload_file,
                    session_name,
                    bundle_path,
                    "cloud_bundle.tar.gz",
                    cancel_check=_cancel_probe,
                )
                _raise_if_aborted()
                if not up_ok:
                    raise _OrchestratorStageError(
                        CloudErrorCode.UPLOAD_FAILED.value,
                        f"Upload mã nguồn lên Colab thất bại: {up_msg}",
                    )
            finally:
                bundle_path.unlink(missing_ok=True)

            if is_url:
                _update(
                    30,
                    f"Đã nhận link: {validated_input.display_name}. Colab sẽ tải video trực tiếp trên cloud...",
                    state="uploading",
                )
                input_arg = validated_input.normalized_value
            else:
                _update(
                    30,
                    f"Đang tải video {validated_input.display_name} lên máy ảo...",
                    state="uploading",
                )
                remote_video_name = sanitize_remote_filename(validated_input.display_name)
                _raise_if_aborted()
                v_ok, v_msg = _call_with_cancel(
                    self.adapter.upload_file,
                    session_name,
                    validated_input.normalized_value,
                    remote_video_name,
                    cancel_check=_cancel_probe,
                )
                _raise_if_aborted()
                if not v_ok:
                    raise _OrchestratorStageError(
                        CloudErrorCode.UPLOAD_FAILED.value,
                        f"Upload video thất bại: {v_msg}",
                    )
                src_check = Path(validated_input.normalized_value)
                if source_snapshot and (
                    not src_check.is_file()
                    or src_check.stat().st_size != source_snapshot["size_bytes"]
                    or _file_sha256(src_check) != source_snapshot["sha256"]
                ):
                    raise _OrchestratorStageError(
                        CloudErrorCode.UPLOAD_VERIFICATION_FAILED.value,
                        "Phát hiện file video nguồn cục bộ bị thay đổi hoặc mất sau bước upload.",
                    )
                input_arg = f"/content/{remote_video_name}"

            # 4. Giải nén và cài đặt môi trường trên VM (`preparing`)
            if not _update(
                40,
                "Đang giải nén và chuẩn bị môi trường AI (Demucs, Whisper Large-v3-turbo, CapCut TTS)...",
                state="preparing",
            ):
                _raise_if_aborted()
                raise _JobAborted("lease_lost", is_lease_lost=True)

            tar_ok, tar_msg = _call_with_cancel(
                self.adapter.exec_argv,
                session_name,
                ["tar", "-xzf", "/content/cloud_bundle.tar.gz", "-C", "/content"],
                cwd="/content",
                cancel_check=_cancel_probe,
            )
            _raise_if_aborted()
            if not tar_ok:
                raise _OrchestratorStageError(
                    CloudErrorCode.UPLOAD_VERIFICATION_FAILED.value,
                    f"Giải nén mã nguồn trên Colab thất bại: {tar_msg}",
                )

            setup_timeout = int(DEFAULT_CONFIG.get("setup_timeout_seconds", 600) or 600)
            min_disk_mb = int(DEFAULT_CONFIG.get("min_remote_disk_mb", 4096) or 4096)
            setup_argv = [
                "python3",
                "setup_env.py",
                "--expected-gpu",
                gpu_type,
                "--min-disk-mb",
                str(min_disk_mb),
                "--verify-bundle",
                "bundle_manifest.json",
                "--pip-timeout",
                str(min(300, setup_timeout)),
            ]
            env_ok, env_msg = _call_with_cancel(
                self.adapter.exec_argv,
                session_name,
                setup_argv,
                cwd="/content",
                timeout=setup_timeout,
                cancel_check=_cancel_probe,
            )
            _raise_if_aborted()
            safe_env_msg = redact_secrets(env_msg, extra_secrets=extra_secrets)
            logger.info("Env setup output:\n%s", safe_env_msg)
            if not env_ok:
                setup_err_code = _extract_setup_error_code(safe_env_msg)
                setup_result = _parse_setup_env_result(safe_env_msg) or {}
                setup_detail = str(setup_result.get("message") or safe_env_msg[-2000:])
                raise _OrchestratorStageError(
                    setup_err_code,
                    f"Cài đặt môi trường AI trên Colab thất bại ({setup_err_code}):\n{setup_detail}",
                )

            # 5. Chạy Pipeline xử lý video chính (`processing`)
            if not _update(
                55,
                "Đang chạy lồng tiếng chuẩn V1 (Demucs tách âm, Whisper GPU, CapCut TTS, EBU R128)...",
                state="processing",
            ):
                _raise_if_aborted()
                raise _JobAborted("lease_lost", is_lease_lost=True)

            pipeline_argv = [
                "/content/.autodub_venv/bin/python"
                if (preflight_info.get("env_manifest") or {}).get("isolated_runtime") else "python3",
                "full_pipeline.py",
                "--input",
                input_arg,
                "--voice",
                voice_choice,
            ]
            job_opts = getattr(job, "options", None) or {}
            if isinstance(job_opts, dict) and job_opts.get("allow_no_speech"):
                pipeline_argv.append("--allow-no-speech")
            env_vars = {"GEMINI_API_KEY": gemini_key} if gemini_key else None
            pipe_ok, pipe_output = _call_with_cancel(
                self.adapter.exec_argv,
                session_name,
                pipeline_argv,
                cwd="/content",
                env_vars=env_vars,
                timeout=DEFAULT_CONFIG["exec_timeout_seconds"],
                cancel_check=_cancel_probe,
            )
            _raise_if_aborted()
            safe_pipe_output = redact_secrets(pipe_output, extra_secrets=extra_secrets)
            logger.info("Pipeline Colab output:\n%s", safe_pipe_output)
            if not pipe_ok:
                pipe_err_code = _extract_pipeline_error_code(safe_pipe_output)
                pipeline_result = _parse_pipeline_result(safe_pipe_output) or {}
                pipeline_detail = str(pipeline_result.get("error") or safe_pipe_output[-2000:])
                raise _OrchestratorStageError(
                    pipe_err_code,
                    f"Pipeline Colab thất bại ({pipe_err_code}):\n{pipeline_detail}",
                )

            # 6. Tải video thành phẩm và kết quả về vùng staging riêng theo job/attempt (`downloading`)
            if not _update(
                85,
                "Đang tải video thành phẩm về máy tính...",
                state="downloading",
            ):
                _raise_if_aborted()
                raise _JobAborted("lease_lost", is_lease_lost=True)

            job_res_dir = Path(DEFAULT_CONFIG["results_dir"]) / job_id
            attempt_res_dir = job_res_dir / f"attempt_{session_attempt}"
            attempt_res_dir.mkdir(parents=True, exist_ok=True)

            _raise_if_aborted()
            dl_ok, dl_path = _call_with_cancel(
                self.adapter.download_file,
                session_name,
                "output_video.mp4",
                attempt_res_dir,
                cancel_check=_cancel_probe,
            )
            _raise_if_aborted()
            if not dl_ok:
                raise _OrchestratorStageError(
                    CloudErrorCode.DOWNLOAD_FAILED.value,
                    "Không tải được video thành phẩm từ Colab về.",
                )

            for remote_art in ("result_metadata.json", "subtitles.srt", "subtitles_vi.srt", "subtitles_vi.ass"):
                if remote_art in _DOWNLOADABLE_REMOTE_ARTIFACTS:
                    _call_with_cancel(
                        self.adapter.download_file,
                        session_name,
                        remote_art,
                        attempt_res_dir,
                        cancel_check=_cancel_probe,
                    )
            _raise_if_aborted()

            # 7. Kiểm tra QC tại vùng staging & xuất thành phẩm atomic an toàn (`validating`)
            _raise_if_aborted()
            if not _update(
                95,
                "Đang kiểm tra QC và xuất video thành phẩm an toàn...",
                state="validating",
            ):
                _raise_if_aborted()
                raise _JobAborted("lease_lost", is_lease_lost=True)

            dl_file = Path(dl_path)
            _raise_if_aborted()
            qc_res = self.validate_downloaded_output(
                dl_file,
                attempt_res_dir,
                source_fingerprint=preflight_info.get("fingerprint"),
                extra_secrets=extra_secrets,
            )
            _raise_if_aborted()

            out_env_dir = os.getenv("AUTODUB_OUTPUT_DIR", r"D:\banve")
            final_out_dir = Path(out_env_dir) if Path(out_env_dir).exists() else attempt_res_dir
            if not is_url and source_snapshot:
                src_final = Path(validated_input.normalized_value)
                if not src_final.is_file() or src_final.stat().st_size != source_snapshot["size_bytes"]:
                    raise _OrchestratorStageError(
                        CloudErrorCode.VALIDATION_FAILED.value,
                        "File video nguồn không còn nguyên vẹn sau quá trình xử lý Cloud.",
                    )

            remote_meta = qc_res.get("remote_metadata") or {}
            local_probe = qc_res.get("local_probe") or {}
            output_manifest: Dict[str, Any] = {
                "schema_version": 1,
                "job_id": job_id,
                "attempt": session_attempt,
                "result_path": None,
                "staging_result_path": str(dl_file),
                "staging_dir": str(attempt_res_dir),
                "sha256": qc_res["sha256"],
                "size_bytes": qc_res["size_bytes"],
                "duration_seconds": (
                    local_probe.get("duration_seconds")
                    or (remote_meta.get("output_video_probe") or {}).get("duration_seconds")
                ),
                "video_codec": (
                    local_probe.get("video_codec")
                    or (remote_meta.get("output_video_probe") or {}).get("video_codec")
                ),
                "audio_codec": (
                    local_probe.get("audio_codec")
                    or (remote_meta.get("output_video_probe") or {}).get("audio_codec")
                ),
                "qc_passed": True,
                "qc_report": qc_res["qc_report"],
                "remote_metadata": remote_meta,
            }
            (attempt_res_dir / "output_manifest.json").write_text(
                redact_secrets(json.dumps(output_manifest, indent=2, ensure_ascii=False), extra_secrets=extra_secrets),
                encoding="utf-8",
            )
            _raise_if_aborted()

            # 8. Thu hồi & xác nhận session trước bước hoàn tất; tính lượng CU ước tính theo thời gian chạy
            elapsed_sec = max(0.0, time.time() - (runtime_start_time or start_time))
            cu_spent = round((elapsed_sec / 3600.0) * rate_per_hour, 3)
            cleanup_res = _finalize_session_once()
            final_cleanup_status = str(
                cleanup_res.get("cleanup_status")
                or ("verified" if cleanup_res.get("verified_stopped") else "failed_retry_needed")
            )
            if not cleanup_res.get("verified_stopped"):
                raise _OrchestratorStageError(
                    CloudErrorCode.CLEANUP_FAILED.value,
                    cleanup_res.get("error") or "Chưa xác nhận đã dừng session Colab; chưa xuất thành phẩm.",
                )

            _raise_if_aborted()
            final_video_path = self.export_deliverable_atomic(
                dl_file,
                final_out_dir,
                job_id=job_id,
                attempt=session_attempt,
                video_name=job.video_name or validated_input.display_name,
                expected_sha256=qc_res["sha256"],
            )
            output_manifest["result_path"] = str(final_video_path)
            output_manifest.pop("staging_result_path", None)
            (attempt_res_dir / "output_manifest.json").write_text(
                redact_secrets(json.dumps(output_manifest, indent=2, ensure_ascii=False), extra_secrets=extra_secrets),
                encoding="utf-8",
            )

            if not _update(
                100,
                f"Hoàn thành! Đã kiểm tra QC & lưu thành phẩm (~{cu_spent} CU ước tính).",
                state="completed",
                result_path=str(final_video_path),
                cu_actual=cu_spent,
                cleanup_status=final_cleanup_status,
                measurement_source=CloudCostMeasurementSource.RUNTIME_RATE_ESTIMATE.value,
                output_manifest=output_manifest,
            ):
                if final_video_path != dl_file:
                    try:
                        final_video_path.unlink(missing_ok=True)
                    except OSError:
                        pass
                    final_video_path = None
                _raise_if_aborted()
                raise _JobAborted("lease_lost", is_lease_lost=True)

            logger.info(
                "=== HOÀN THÀNH CLOUD JOB %s trong %.1fs (Ước tính tiêu thụ ~%.3f CU) ===",
                job_id,
                elapsed_sec,
                cu_spent,
            )
            return True

        except _JobAborted as abort_exc:
            if abort_exc.is_lease_lost:
                logger.warning(
                    "Cloud Job %s (attempt=%d) mất quyền sở hữu (%s); dừng thực thi an toàn không ghi đè.",
                    job_id,
                    session_attempt,
                    abort_exc.reason,
                )
                return False

            cu_spent = _estimate_elapsed_cu()
            if abort_exc.is_budget_exceeded:
                logger.warning(
                    "Cloud Job %s bị dừng sớm do ngân sách/số dư (%s); thu hồi session %s...",
                    job_id,
                    abort_exc.reason,
                    session_name,
                )
                _update(
                    0,
                    "Đang dừng máy ảo Colab do chạm hạn mức chi phí...",
                    state="cancelling",
                    cu_actual=cu_spent,
                    allow_from_cancel=True,
                )
                cleanup_res = _finalize_session_once()
                err_code = (
                    CloudErrorCode.BUDGET_UNCERTAIN.value
                    if abort_exc.reason == "budget_uncertain"
                    else CloudErrorCode.BUDGET_EXCEEDED.value
                )
                self.store.update_progress(
                    job_id,
                    state="failed",
                    percent=0,
                    step_name="Đã dừng do chạm hạn mức chi phí/số dư CU.",
                    error=budget_abort_reason or "Đã chạm hạn mức chi phí CU khi đang xử lý.",
                    error_code=err_code,
                    cu_actual=cu_spent,
                    cleanup_status=str(
                        cleanup_res.get("cleanup_status")
                        or ("verified" if cleanup_res.get("verified_stopped") else "failed_retry_needed")
                    ),
                    owner_id=effective_owner,
                    attempt=ownership_attempt,
                    allow_from_cancel=True,
                    measurement_source=CloudCostMeasurementSource.RUNTIME_RATE_ESTIMATE.value,
                )
                return False

            logger.info("Cloud Job %s nhận yêu cầu hủy (%s), đang thu hồi session %s...", job_id, abort_exc.reason, session_name)
            _update(
                0,
                "Đang dừng và xác nhận thu hồi máy ảo Colab...",
                state="cancelling",
                cu_actual=cu_spent,
                allow_from_cancel=True,
            )
            cleanup_res = _finalize_session_once()
            if cleanup_res.get("verified_stopped"):
                self.store.update_progress(
                    job_id,
                    state="cancelled",
                    percent=0,
                    step_name="Đã dừng theo yêu cầu người dùng.",
                    error="Đã dừng bởi người dùng.",
                    error_code=CloudErrorCode.CANCELLED_BY_USER.value,
                    cu_actual=cu_spent,
                    cleanup_status="verified",
                    owner_id=effective_owner,
                    attempt=ownership_attempt,
                    allow_from_cancel=True,
                    measurement_source=CloudCostMeasurementSource.RUNTIME_RATE_ESTIMATE.value,
                )
            else:
                self.store.update_progress(
                    job_id,
                    state="cancelling",
                    percent=0,
                    step_name="Chưa xác nhận dừng máy ảo Colab; cần dọn dẹp lại.",
                    error=cleanup_res.get("error") or "Chưa xác nhận dừng session Colab.",
                    error_code=CloudErrorCode.CLEANUP_FAILED.value,
                    cu_actual=cu_spent,
                    cleanup_status=str(cleanup_res.get("cleanup_status") or "failed_retry_needed"),
                    owner_id=effective_owner,
                    attempt=ownership_attempt,
                    allow_from_cancel=True,
                    measurement_source=CloudCostMeasurementSource.RUNTIME_RATE_ESTIMATE.value,
                )
            return False

        except Exception as exc:
            # Nếu trong lúc step fail mà job vừa có yêu cầu hủy hoặc mất lease thì ưu tiên nhánh đó
            cu_spent = _estimate_elapsed_cu()
            aborted_now, abort_reason = _check_abort()
            if aborted_now:
                if abort_reason in _LEASE_LOST_REASONS:
                    logger.warning("Job %s mất lease sau lỗi bước (%s); không ghi đè trạng thái.", job_id, abort_reason)
                    return False
                _update(
                    0,
                    "Đang dừng và xác nhận thu hồi máy ảo Colab...",
                    state="cancelling",
                    cu_actual=cu_spent,
                    allow_from_cancel=True,
                )
                cleanup_res = _finalize_session_once()
                if cleanup_res.get("verified_stopped"):
                    self.store.update_progress(
                        job_id,
                        state="cancelled",
                        percent=0,
                        step_name="Đã dừng theo yêu cầu người dùng.",
                        error="Đã dừng bởi người dùng.",
                        error_code=CloudErrorCode.CANCELLED_BY_USER.value,
                        cu_actual=cu_spent,
                        cleanup_status="verified",
                        owner_id=effective_owner,
                        attempt=ownership_attempt,
                        allow_from_cancel=True,
                        measurement_source=CloudCostMeasurementSource.RUNTIME_RATE_ESTIMATE.value,
                    )
                return False

            cleanup_res = _finalize_session_once()
            safe_exc = redact_secrets(str(exc), extra_secrets=extra_secrets)
            exc_error_code = (
                exc.error_code
                if isinstance(exc, _OrchestratorStageError) and exc.error_code in _VALID_ERROR_CODES
                else CloudErrorCode.EXEC_FAILED.value
            )
            logger.error("Lỗi trong quá trình xử lý Cloud Job %s [%s]: %s", job_id, exc_error_code, safe_exc)
            _update(
                0,
                f"Lỗi: {safe_exc[:120]}",
                state="failed",
                error=safe_exc,
                error_code=exc_error_code,
                cu_actual=cu_spent,
                cleanup_status=str(
                    cleanup_res.get("cleanup_status")
                    or ("verified" if cleanup_res.get("verified_stopped") else "failed_retry_needed")
                ),
                measurement_source=CloudCostMeasurementSource.RUNTIME_RATE_ESTIMATE.value,
            )
            return False

        finally:
            if session_may_exist and not cleanup_done:
                try:
                    _finalize_session_once()
                except Exception as stop_exc:
                    logger.warning(
                        "Lỗi thu hồi session %s: %s",
                        session_name,
                        redact_secrets(str(stop_exc), extra_secrets=extra_secrets),
                    )
