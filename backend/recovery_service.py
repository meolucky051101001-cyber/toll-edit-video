"""Build and validate recovery plans from exact sources and V1 manifests."""
from __future__ import annotations

import json
import threading
import time
import uuid
from pathlib import Path

from batch_plan_service import VIDEO_EXTENSIONS, bare_name, fingerprint, output_receipt_path
from config.paths import AppPaths
from pipeline_v2.atomic_io import atomic_write_json
from pipeline_v2.interprocess_lock import CrossProcessLock
from queue_control_service import valid_id
from v1_checkpoint import ManifestManager

_ACTIVE_PLAN_TOKENS = {}
_TOKEN_LOCK = threading.Lock()
TOKEN_TTL_SECONDS = 900

STAGES = {
    "extract_audio": "Trích xuất âm thanh",
    "separate_vocals": "Bóc tách giọng và nhạc nền",
    "speech_asr": "Nhận dạng giọng nói",
    "visual_ocr": "Quét phụ đề trên hình",
    "ai_translation": "Dịch phụ đề",
    "ai_tts": "Lồng tiếng",
    "audio_mixing": "Hòa âm",
    "video_rendering": "Xuất video",
}


def compute_file_fingerprint(file_path):
    try:
        identity = fingerprint(file_path)
        return identity["sha256"] if identity["size"] else None
    except (OSError, ValueError):
        return None


def _stem(name):
    path = Path(name)
    return path.stem if path.suffix.lower() in VIDEO_EXTENSIONS else name


def _source_name_matches(reference, name):
    if Path(reference).suffix.lower() in VIDEO_EXTENSIONS:
        return reference.casefold() == name.casefold()
    return reference.casefold() == _stem(name).casefold()


class RecoveryService:
    def __init__(self, workspace_path=None):
        self.paths = AppPaths.from_environment(Path(__file__).resolve().parents[1])
        self.workspace = Path(workspace_path or self.paths.workspace).resolve()
        self.control_dir = self.workspace / "control"
        self.control_dir.mkdir(parents=True, exist_ok=True)
        self.state_file = self.control_dir / "job_recovery_states.json"
        self.lock_file = self.control_dir / "recovery.lock"
        self.manifests = ManifestManager(self.workspace)

    def _read_states(self):
        try:
            states = json.loads(self.state_file.read_text(encoding="utf-8-sig"))
        except FileNotFoundError:
            return {}
        if not isinstance(states, dict):
            raise ValueError("Trạng thái khôi phục không hợp lệ.")
        return states

    def _source_roots(self, input_dir):
        input_root = Path(input_dir or self.paths.input_dir).resolve()
        return list(dict.fromkeys(
            root.resolve() for root in (input_root, input_root / "processed",
                                        self.workspace / "downloads", self.workspace / "input")
        ))

    @staticmethod
    def _exact_sources(roots, reference):
        sources = set()
        for root in roots:
            if root.is_dir():
                sources.update(
                    path.resolve() for path in root.iterdir()
                    if path.is_file() and path.suffix.lower() in VIDEO_EXTENSIONS
                    and path.resolve().parent == root and _source_name_matches(reference, path.name)
                )
        return sources

    def _manifests_for_name(self, reference):
        matches = []
        for path in self.manifests.manifest_dir.glob("*.manifest.json"):
            record = self.manifests.load_manifest(path.name.removesuffix(".manifest.json"))
            if record and _source_name_matches(reference, record.video_name):
                matches.append(record)
        return matches

    def _resolve(self, reference, input_dir):
        roots = self._source_roots(input_dir)
        if valid_id(reference) and self.manifests._manifest_file(reference).exists():
            manifest = self.manifests.load_manifest(reference)
            if manifest is None or manifest.job_id != reference:
                return None, None, "Manifest của job không đọc được."
            previous = Path(manifest.video_path).resolve()
            if previous.parent in roots and previous.is_file():
                return previous, manifest, None
            sources = self._exact_sources(roots, manifest.video_name)
            archived = [source for source in sources if compute_file_fingerprint(source) == manifest.video_fingerprint]
            if len(archived) == 1:
                return archived[0], manifest, None
            return None, manifest, "Không xác minh được một nguồn duy nhất cho manifest."

        sources = self._exact_sources(roots, reference)
        if not sources:
            return None, None, "Không tìm thấy video nguồn trùng tên chính xác."
        records = self._manifests_for_name(reference)
        if len(sources) > 1:
            return None, None, "Có nhiều video trùng tên. Cần chọn thư mục nguồn hoặc dùng mã job."
        source = next(iter(sources))
        source_sha = compute_file_fingerprint(source)
        related = [record for record in records
                   if Path(record.video_path).resolve() == source or record.video_fingerprint == source_sha]
        if len(related) > 1:
            return source, None, "Có nhiều manifest cho video này. Cần dùng mã job để khôi phục."
        return source, related[0] if related else None, None

    def _work_dir(self, manifest):
        configured = manifest.effective_config.get("output_dir") if manifest else None
        if not configured:
            return None
        path = Path(configured)
        resolved = path.resolve()
        if not path.is_absolute() or path.is_symlink() or resolved.parent != self.workspace or not resolved.is_dir():
            return None
        for candidate in self.manifests.manifest_dir.glob("*.manifest.json"):
            other_id = candidate.name.removesuffix(".manifest.json")
            if other_id == manifest.job_id:
                continue
            other = self.manifests.load_manifest(other_id)
            other_path = other.effective_config.get("output_dir") if other else None
            if other_path and Path(other_path).resolve() == resolved:
                return None
        return resolved

    def _output_status(self, destination, manifest, source_sha):
        if destination.is_symlink() or destination.resolve().parent != destination.parent:
            return "unverified", None
        if not destination.exists():
            return None, None
        output_sha = compute_file_fingerprint(destination)
        if not output_sha:
            return "unverified", output_sha
        try:
            receipt = json.loads(output_receipt_path(self.workspace, destination).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            receipt = {}
        if (manifest and isinstance(receipt, dict) and receipt.get("job_id") == manifest.job_id
                and receipt.get("input_sha256") == source_sha and receipt.get("output_sha256") == output_sha):
            return "verified", output_sha
        if (manifest and manifest.status == "completed" and manifest.output_video_path
                and Path(manifest.output_video_path).resolve() == destination
                and manifest.video_fingerprint == source_sha):
            rendered = manifest.stages.get("video_rendering", {}).get("artifacts", {}).get("temp_render", {})
            if rendered.get("sha256") == output_sha:
                return "verified", output_sha
        return "unverified", output_sha

    def _inspect(self, job_id, input_dir, output_dir):
        reference = bare_name(job_id)
        source, manifest, problem = self._resolve(reference, input_dir)
        identity = manifest.job_id if manifest else _stem(reference)
        retry = self._read_states().get(identity, {}).get("retry_count", 0)
        warnings = [problem] if problem else []
        source_sha = compute_file_fingerprint(source) if source else None
        work_dir = self._work_dir(manifest)
        changed = bool(manifest and source_sha and manifest.video_fingerprint != source_sha)
        if changed:
            warnings.append("Video nguồn đã thay đổi nội dung. Hãy chạy lại bằng một job mới.")
        if source_sha and not manifest and not problem:
            warnings.append("Chưa có manifest gắn với video nguồn. Chạy lại sẽ dùng thư mục làm việc mới.")
        if manifest and not work_dir:
            warnings.append("Không xác minh được thư mục làm việc riêng của manifest.")

        verified = set()
        if manifest and work_dir and source_sha and not changed:
            _, _, details = self.manifests.verify_manifest_resumability(
                manifest.job_id, source_path=source, work_dir=work_dir,
            )
            verified = set(details.get("verified_stages", []))

        destination = None
        output_state = output_sha = None
        if source:
            output_root = Path(output_dir or self.paths.output_dir).resolve()
            destination = output_root / f"Dubbed_{source.stem.removeprefix('Dubbed_')}.mp4"
            output_state, output_sha = self._output_status(destination, manifest, source_sha)
        if output_state == "verified":
            warnings.append("Thành phẩm đã xác minh. Chạy lại có thể thay thế thành phẩm sau khi render thành công.")
        elif output_state == "unverified":
            warnings.append("Đích đã có file chưa xác minh được quyền sở hữu. Hãy chọn thư mục thành phẩm khác.")
        if retry >= 3:
            warnings.append(f"Video này đã được thử lại {retry} lần.")

        ready = bool(source_sha and not problem and output_state != "unverified")
        stages = [
            {"stage_id": stage, "name": label, "status": "reusable" if stage in verified else "rerun",
             "reason": "Đã kiểm tra SHA-256 theo manifest." if stage in verified else "Chưa xác minh được artifact theo manifest.",
             "artifacts": [Path(item["path"]).name for item in manifest.stages[stage]["artifacts"].values()] if stage in verified else []}
            for stage, label in STAGES.items()
        ]
        pending = [stage for stage in STAGES if stage not in verified]
        data = {
            "job_id": identity,
            "display_name": source.name if source else reference,
            "source_file": {"filename": source.name if source else reference,
                            "path": str(source) if source else "Không tìm thấy trên đĩa",
                            "size_mb": round(source.stat().st_size / (1024 * 1024), 2) if source else 0,
                            "fingerprint": source_sha},
            "workspace_dir": str(work_dir) if work_dir else None,
            "output_path": str(destination) if destination else None,
            "can_unpause": False,
            "can_recover": ready and bool(verified) and retry < 3,
            "can_restart": ready,
            "is_completed": output_state == "verified",
            "completed_output": str(destination) if output_state == "verified" else None,
            "retry_count": retry,
            "max_retries": 3,
            "summary": {"total_stages": len(STAGES), "reusable_stages": len(verified),
                        "rerun_stages": len(pending), "first_rerun_stage": pending[0] if pending else "none"},
            "stages": stages,
            "warnings": warnings,
        }
        basis = (identity, source, source_sha, work_dir, manifest.updated_at if manifest else None,
                 destination, output_state, output_sha, tuple(pending), retry)
        return data, basis, manifest

    def build_resume_plan(self, job_id, input_dir=None, output_dir=None):
        data, basis, _ = self._inspect(job_id, input_dir, output_dir)
        token = "plan_" + uuid.uuid4().hex
        now = time.monotonic()
        with _TOKEN_LOCK:
            for previous, record in list(_ACTIVE_PLAN_TOKENS.items()):
                if now - record["created_at"] > TOKEN_TTL_SECONDS:
                    _ACTIVE_PLAN_TOKENS.pop(previous)
            if len(_ACTIVE_PLAN_TOKENS) >= 256:
                _ACTIVE_PLAN_TOKENS.pop(next(iter(_ACTIVE_PLAN_TOKENS)))
            _ACTIVE_PLAN_TOKENS[token] = {"reference": job_id, "created_at": now, "workspace": self.workspace, "basis": basis}
        return {**data, "plan_token": token}

    def execute_resume_action(self, job_id, mode, plan_token=None, config_overrides=None, input_dir=None, output_dir=None):
        if mode not in ("recover", "restart"):
            raise ValueError("Chế độ khôi phục không hợp lệ.")
        with CrossProcessLock(self.lock_file, name="Recovery", timeout_seconds=1):
            with _TOKEN_LOCK:
                record = _ACTIVE_PLAN_TOKENS.get(plan_token)
            if (not record or record["reference"] != job_id or record["workspace"] != self.workspace
                    or time.monotonic() - record["created_at"] > TOKEN_TTL_SECONDS):
                raise ValueError("Mã kế hoạch khôi phục không hợp lệ hoặc đã hết hạn.")
            plan, basis, manifest = self._inspect(job_id, input_dir, output_dir)
            if basis != record["basis"]:
                raise RuntimeError("Nguồn, manifest hoặc đích đã thay đổi. Hãy tải lại kế hoạch.")
            if not plan["source_file"]["fingerprint"]:
                raise FileNotFoundError("Không xác minh được video nguồn.")
            if mode == "recover":
                if plan["retry_count"] >= 3:
                    raise RuntimeError("Tác vụ đã vượt quá giới hạn thử lại tối đa.")
                if not plan["can_recover"] or config_overrides:
                    raise RuntimeError("Cần manifest hợp lệ và cấu hình đã đóng băng để khôi phục.")
                pipeline_id = manifest.job_id
                work_dir = Path(plan["workspace_dir"])
                frozen = manifest.effective_config
            else:
                if not plan["can_restart"]:
                    raise RuntimeError("Cần xác minh lại nguồn và đích trước khi chạy lại.")
                pipeline_id = "recovery_" + uuid.uuid4().hex
                work_dir = self.workspace / ("batch_" + pipeline_id)
                frozen = config_overrides

            identity = plan["job_id"]
            retry = plan["retry_count"] + 1 if mode == "recover" else 0
            first = plan["summary"]["first_rerun_stage"] if mode == "recover" else next(iter(STAGES))
            states = self._read_states()
            states[identity] = {"source_fingerprint": plan["source_file"]["fingerprint"], "retry_count": retry,
                                "last_mode": mode, "checkpoint_stage": first, "updated_at": time.time()}
            atomic_write_json(self.state_file, states)
            with _TOKEN_LOCK:
                _ACTIVE_PLAN_TOKENS.pop(plan_token, None)
            return {"status": "success", "mode": mode, "job_id": identity, "pipeline_job_id": pipeline_id,
                    "resume_from_stage": first, "retry_count": retry, "source_video": plan["source_file"]["path"],
                    "source_fingerprint": plan["source_file"]["fingerprint"], "work_dir": str(work_dir), "frozen_config": frozen,
                    "message": f"Đã chuẩn bị {mode} cho {plan['display_name']}."}
