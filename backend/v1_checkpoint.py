"""
v1_checkpoint.py - Content-validated sidecars & Atomic Job Manifest cho Tool V1 (Giai đoạn 3).
Bảo toàn 100% tính năng Checkpoint cũ, đồng thời bổ sung:
- JobManifest nguyên tử lưu trữ trạng thái từng Stage, fingerprint video, mô hình hiệu lực.
- Lưu trữ tiến độ theo lô (batch progress) cho Translation & TTS để không bao giờ phải dịch hay lồng tiếng lại từ đầu.
- Xác thực tính hợp lệ của artifacts trước khi quyết định resume an toàn.
"""

from __future__ import annotations

import os
import sys
import json
import time
import uuid
import hashlib
import tempfile
import logging
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# ===== 1. TƯƠNG THÍCH NGƯỢC (LEGACY CHECKPOINT & FINGERPRINT) =====

def fingerprint(path: str | Path) -> str:
    """Tính SHA256 digest của file để xác thực tính toàn vẹn."""
    digest = hashlib.sha256()
    with open(path, "rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


class Checkpoint:
    """Legacy Checkpoint class được giữ nguyên 100% để đảm bảo tương thích ngược."""
    def __init__(self, source, outputs, settings):
        self.outputs = [Path(p) for p in outputs]
        self.path = Path(str(self.outputs[0]) + ".v1-cache.json")
        self.key = None
        try:
            self.key = [fingerprint(source), settings]
        except OSError:
            pass

    def artifacts(self):
        if any(not p.is_file() or p.stat().st_size == 0 for p in self.outputs):
            raise ValueError("Missing or empty artifact")
        return [[str(p.resolve()), fingerprint(p)] for p in self.outputs]

    def hit(self):
        if self.key is None:
            return False
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            return data["key"] == self.key and data["outputs"] == self.artifacts()
        except (OSError, ValueError, KeyError, TypeError):
            return False

    def save(self):
        if self.key is None:
            return
        temporary = None
        try:
            data = {"key": self.key, "outputs": self.artifacts()}
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=self.path.parent, delete=False) as stream:
                temporary = stream.name
                json.dump(data, stream)
            os.replace(temporary, self.path)
        except (OSError, ValueError):
            pass
        finally:
            if temporary:
                try:
                    os.unlink(temporary)
                except OSError:
                    pass


# ===== 2. ATOMIC JOB MANIFEST CHO GIAI ĐOẠN 3 =====

STAGES_ORDER = [
    "extract_audio",
    "separate_vocals",
    "speech_asr",
    "visual_ocr",
    "ai_translation",
    "ai_tts",
    "audio_mixing",
    "video_rendering",
]


@dataclass
class JobManifest:
    job_id: str
    video_name: str
    video_path: str
    video_fingerprint: str
    video_mode: str = "SHORT"
    status: str = "created"  # created, in_progress, completed, failed, paused
    current_stage: str = "extract_audio"
    stages: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    tts_completed_segments: List[int] = field(default_factory=list)
    effective_config: Dict[str, Any] = field(default_factory=dict)
    routing_snapshot: Dict[str, Any] = field(default_factory=dict)
    output_video_path: Optional[str] = None
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    error_message: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


class ManifestManager:
    """Quản lý đọc/ghi manifest nguyên tử (atomic) chống hỏng file khi mất điện/crash."""

    def __init__(self, workspace_path: Optional[str | Path] = None):
        if workspace_path:
            self.workspace = Path(workspace_path)
        else:
            base_dir = Path(__file__).resolve().parent
            self.workspace = Path(os.getenv("AUTODUB_WORKSPACE", str(base_dir.parent / "workspace")))
        self.manifest_dir = self.workspace / "bot_system" / "manifests"
        self.manifest_dir.mkdir(parents=True, exist_ok=True)

    def _manifest_file(self, job_id: str) -> Path:
        clean_id = "".join(c for c in job_id if c.isalnum() or c in ("-", "_")).strip()[:120]
        return self.manifest_dir / f"{clean_id}.manifest.json"

    def save_manifest(self, manifest: JobManifest) -> None:
        """Ghi manifest an toàn qua file tạm và atomic rename với retry trên Windows."""
        manifest.updated_at = time.time()
        file_path = self._manifest_file(manifest.job_id)
        temp_file = file_path.with_suffix(".tmp." + uuid.uuid4().hex[:8])
        try:
            temp_file.write_text(
                json.dumps(manifest.to_dict(), ensure_ascii=False, indent=2),
                encoding="utf-8"
            )
            # Thử lại 5 lần nếu Windows bị khóa chia sẻ file tạm thời ([WinError 5] Access is denied)
            for rep_attempt in range(5):
                try:
                    os.replace(temp_file, file_path)
                    break
                except (PermissionError, OSError) as rep_err:
                    if rep_attempt == 4:
                        raise
                    time.sleep(0.06 * (rep_attempt + 1))
        except Exception as exc:
            logger.error("Lỗi ghi manifest nguyên tử cho job %s: %s", manifest.job_id, exc)
            if temp_file.is_file():
                try: temp_file.unlink()
                except OSError: pass
            raise RuntimeError(f"Cannot persist checkpoint for job {manifest.job_id}") from exc

    def load_manifest(self, job_id: str) -> Optional[JobManifest]:
        """Tải manifest đã lưu của job."""
        file_path = self._manifest_file(job_id)
        if not file_path.is_file():
            return None
        try:
            data = json.loads(file_path.read_text(encoding="utf-8"))
            return JobManifest(**data)
        except Exception as exc:
            logger.warning("Không thể đọc manifest %s: %s", file_path.name, exc)
            return None

    def get_or_create_manifest(
        self,
        job_id: str,
        video_path: str | Path,
        video_mode: str = "AUTO",
        effective_config: Optional[Dict[str, Any]] = None,
        routing_snapshot: Optional[Dict[str, Any]] = None,
    ) -> JobManifest:
        """Lấy manifest cũ nếu trùng fingerprint hoặc tạo mới."""
        existing = self.load_manifest(job_id)
        path = Path(video_path)
        v_fingerprint = fingerprint(path) if path.is_file() else ""

        if existing and existing.video_fingerprint == v_fingerprint:
            return existing

        initial_stages = {
            s: {
                "status": "pending",
                "started_at": None,
                "completed_at": None,
                "duration_s": 0.0,
                "artifacts": {},
                "retry_count": 0,
                "error": None,
            }
            for s in STAGES_ORDER
        }

        manifest = JobManifest(
            job_id=job_id,
            video_name=path.name,
            video_path=str(path.resolve()) if path.is_file() else str(video_path),
            video_fingerprint=v_fingerprint,
            video_mode=video_mode,
            status="created",
            current_stage=STAGES_ORDER[0],
            stages=initial_stages,
            tts_completed_segments=[],
            effective_config=effective_config or {},
            routing_snapshot=routing_snapshot or {},
        )
        self.save_manifest(manifest)
        return manifest

    def record_stage_start(self, job_id: str, stage_id: str) -> None:
        manifest = self.load_manifest(job_id)
        if not manifest:
            return
        manifest.status = "in_progress"
        manifest.current_stage = stage_id
        if stage_id in manifest.stages:
            st = manifest.stages[stage_id]
            st["status"] = "running"
            st["started_at"] = time.time()
            st["retry_count"] = st.get("retry_count", 0) + 1
        self.save_manifest(manifest)

    def record_stage_complete(
        self,
        job_id: str,
        stage_id: str,
        artifacts: Optional[Dict[str, str]] = None,
    ) -> None:
        manifest = self.load_manifest(job_id)
        if not manifest:
            return
        if stage_id in manifest.stages:
            st = manifest.stages[stage_id]
            now = time.time()
            st["status"] = "completed"
            st["completed_at"] = now
            if st.get("started_at"):
                st["duration_s"] = round(now - st["started_at"], 2)
            if artifacts:
                # Ghi nhận artifacts và hash xác thực
                st_artifacts = {}
                for k, art_path in artifacts.items():
                    p = Path(art_path)
                    if p.is_file() and p.stat().st_size > 0:
                        st_artifacts[k] = {
                            "path": str(p.resolve()),
                            "size_bytes": p.stat().st_size,
                            "sha256": fingerprint(p),
                        }
                st["artifacts"] = st_artifacts

        # Cập nhật stage tiếp theo
        try:
            curr_idx = STAGES_ORDER.index(stage_id)
            if curr_idx + 1 < len(STAGES_ORDER):
                manifest.current_stage = STAGES_ORDER[curr_idx + 1]
            else:
                manifest.status = "awaiting_qc"
        except ValueError:
            pass

        self.save_manifest(manifest)

    def record_stage_failure(self, job_id: str, stage_id: str, error_msg: str) -> None:
        manifest = self.load_manifest(job_id)
        if not manifest:
            return
        manifest.status = "failed"
        manifest.error_message = error_msg
        if stage_id in manifest.stages:
            st = manifest.stages[stage_id]
            st["status"] = "failed"
            st["error"] = error_msg
            if st.get("started_at"):
                st["duration_s"] = round(time.time() - st["started_at"], 2)
        self.save_manifest(manifest)

    def record_tts_segment_done(self, job_id: str, seg_index: int) -> None:
        """Ghi nhận segment TTS đã được tạo thành công vào manifest."""
        manifest = self.load_manifest(job_id)
        if manifest and seg_index not in manifest.tts_completed_segments:
            manifest.tts_completed_segments.append(seg_index)
            self.save_manifest(manifest)

    def verify_manifest_resumability(self, job_id: str) -> Tuple[bool, str, Dict[str, Any]]:
        """
        Kiểm tra xem job có thể resume từ stage nào mà không phải chạy lại từ đầu:
        - Kiểm tra fingerprint file video nguồn (nếu file đã bị sửa đổi -> không thể resume an toàn).
        - Kiểm tra tính hợp lệ của từng artifact ở các stage trước đó.
        - Trả về (can_resume, resume_from_stage, plan_details).
        """
        manifest = self.load_manifest(job_id)
        if not manifest:
            return False, STAGES_ORDER[0], {"reason": "Không tìm thấy manifest cho job"}

        if manifest.status == "completed":
            return False, "completed", {"reason": "Job đã hoàn tất toàn bộ"}

        # 1. Kiểm tra video nguồn
        source_p = Path(manifest.video_path)
        if not source_p.is_file():
            return False, STAGES_ORDER[0], {"reason": f"Không tìm thấy video nguồn tại: {source_p}"}

        curr_fingerprint = fingerprint(source_p)
        if curr_fingerprint != manifest.video_fingerprint:
            return False, STAGES_ORDER[0], {"reason": "Video nguồn đã bị thay đổi nội dung (fingerprint mismatch)"}

        # 2. Quét từ stage đầu đến cuối xem stage nào artifact còn nguyên vẹn
        resume_stage = STAGES_ORDER[0]
        verified_stages = []

        for s_id in STAGES_ORDER:
            st = manifest.stages.get(s_id, {})
            if st.get("status") != "completed":
                resume_stage = s_id
                break

            # Xác thực tính toàn vẹn của artifacts đã lưu
            artifacts = st.get("artifacts", {})
            stage_valid = bool(artifacts)
            # New-format jobs cannot resume from an ASS file alone or lose OCR
            # geometry. Older manifests are safely recomputed at these stages.
            required = {"separate_vocals": {"vocals_audio", "no_vocals_audio"},
                        "visual_ocr": {"ocr_data"},
                        "ai_translation": {"translation_data"},
                        "ai_tts": {"tts_data", "ass_path", "voice_lock"}}
            if s_id in required and not required[s_id].issubset(artifacts):
                stage_valid = False
            for art_k, art_info in artifacts.items():
                art_p = Path(art_info.get("path", ""))
                if not art_p.is_file() or art_p.stat().st_size == 0:
                    stage_valid = False
                    break
                # Kiểm tra sha256
                if fingerprint(art_p) != art_info.get("sha256"):
                    stage_valid = False
                    break

            if stage_valid:
                verified_stages.append(s_id)
            else:
                resume_stage = s_id
                break

        if len(verified_stages) == len(STAGES_ORDER):
            resume_stage = STAGES_ORDER[-1]
        can_resume = len(verified_stages) > 0
        plan = {
            "can_resume": can_resume,
            "resume_from_stage": resume_stage,
            "verified_stages": verified_stages,
            "tts_cached_segments": len(manifest.tts_completed_segments),
            "video_mode": manifest.video_mode,
        }
        return can_resume, resume_stage, plan
