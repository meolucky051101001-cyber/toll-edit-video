"""
Job Config Service - Quản lý cấu hình nháp cho từng video và snapshot cấu hình đóng băng khi chạy job.
Lưu trữ:
- Bản nháp (draft overrides): workspace/control/video_draft_configs.json
- Cấu hình đóng băng (frozen job configs): workspace/control/job_frozen_configs.json
"""

import os
import sys
import json
import time
import uuid
import sqlite3
import hashlib
from contextlib import contextmanager
from copy import deepcopy
from pathlib import Path
from typing import Dict, Any, List, Optional

ROOT_DIR = Path(__file__).resolve().parent
WORKSPACE_DIR = Path(os.getenv("AUTODUB_WORKSPACE", str(ROOT_DIR.parent / "workspace")))
CONTROL_DIR = WORKSPACE_DIR / "control"

DRAFTS_FILE = CONTROL_DIR / "video_draft_configs.json"
FROZEN_FILE = CONTROL_DIR / "job_frozen_configs.json"

_IDEMPOTENCY_TTL_SEC = 300.0  # 5 minutes


def check_idempotency(key: str) -> Optional[Dict[str, Any]]:
    with _idempotency_db() as db:
        row = db.execute("SELECT result FROM requests WHERE key=? AND created_at>?",
                         (key, time.time() - _IDEMPOTENCY_TTL_SEC)).fetchone()
        return json.loads(row[0]) if row else None


def record_idempotency(key: str, result: Dict[str, Any]):
    with _idempotency_db() as db:
        db.execute("DELETE FROM requests WHERE created_at<=?", (time.time() - _IDEMPOTENCY_TTL_SEC,))
        db.execute("INSERT OR REPLACE INTO requests VALUES (?, ?, ?)",
                   (key, json.dumps(result, ensure_ascii=False), time.time()))


@contextmanager
def _idempotency_db():
    CONTROL_DIR.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(CONTROL_DIR / "config_requests.sqlite3", timeout=15)
    try:
        with db:
            db.execute("CREATE TABLE IF NOT EXISTS requests (key TEXT PRIMARY KEY, result TEXT NOT NULL, created_at REAL NOT NULL)")
            yield db
    finally:
        db.close()


def idempotency_lock(key: str):
    digest = hashlib.sha256(key.encode("utf-8")).hexdigest()
    return _file_lock(CONTROL_DIR / f"request-{digest}.lock")


try:
    from preset_service import resolve_effective_config, get_preset
except ImportError:
    from .preset_service import resolve_effective_config, get_preset


def _atomic_write_json(file_path: Path, data: Any):
    try:
        from pipeline_v2.atomic_io import atomic_write_json
    except ImportError:
        from .pipeline_v2.atomic_io import atomic_write_json
    atomic_write_json(file_path, data)


def _file_lock(path: Path):
    try:
        from pipeline_v2.interprocess_lock import CrossProcessLock
    except ImportError:
        from .pipeline_v2.interprocess_lock import CrossProcessLock
    return CrossProcessLock(path, name="JobConfig", timeout_seconds=15, poll_interval=0.02)


@contextmanager
def _config_transaction(path: Path):
    with _file_lock(path.with_name(path.name + ".lock")):
        # Refuse to replace a damaged store with an empty default.
        data = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
        if not isinstance(data, dict):
            raise ValueError(f"Invalid job configuration store: {path}")
        yield data
        _atomic_write_json(path, data)


def _frozen_file_for_workspace(workspace_path: Optional[str | Path] = None) -> Path:
    if workspace_path is None:
        return FROZEN_FILE
    file_path = Path(workspace_path) / "control" / "job_frozen_configs.json"
    return file_path


def _frozen_files_for_lookup(
    path_or_key: str,
    workspace_path: Optional[str | Path] = None,
) -> List[Path]:
    if workspace_path is not None:
        return [_frozen_file_for_workspace(workspace_path)]

    candidates: List[Path] = []
    candidate_path = Path(path_or_key)
    if candidate_path.is_absolute():
        for parent in candidate_path.parents:
            scoped_file = parent / "control" / "job_frozen_configs.json"
            if scoped_file.is_file() and scoped_file not in candidates:
                candidates.append(scoped_file)
    if FROZEN_FILE not in candidates:
        candidates.append(FROZEN_FILE)
    return candidates


def _read_json_safe(file_path: Path, default_val: Any) -> Any:
    if not file_path.exists():
        return default_val
    try:
        return json.loads(file_path.read_text(encoding="utf-8"))
    except Exception:
        return default_val


# ===== DRAFTS (Bản nháp cấu hình riêng trước khi chạy) =====

def list_all_drafts() -> Dict[str, Any]:
    """Lấy toàn bộ danh sách bản nháp cấu hình riêng theo tên video."""
    return _read_json_safe(DRAFTS_FILE, {})


def get_video_draft(video_name: str) -> Optional[Dict[str, Any]]:
    """Lấy bản nháp của 1 video cụ thể."""
    drafts = list_all_drafts()
    return drafts.get(video_name)


def save_video_draft(video_name: str, draft_data: Dict[str, Any]) -> Dict[str, Any]:
    """Lưu hoặc cập nhật bản nháp cấu hình riêng cho 1 video."""
    with _config_transaction(DRAFTS_FILE) as drafts:
        now = time.time()
        current = drafts.get(video_name, {"video_name": video_name, "created_at": now, "revision": 0})
        current["preset_id"] = draft_data.get("preset_id")
        current["overrides"] = draft_data.get("overrides", {})
        current["revision"] = current.get("revision", 0) + 1
        current["updated_at"] = now
        drafts[video_name] = current
    return current


def delete_video_draft(video_name: str) -> bool:
    """Xóa bản nháp cấu hình riêng của video."""
    with _config_transaction(DRAFTS_FILE) as drafts:
        return drafts.pop(video_name, None) is not None


# ===== FROZEN CONFIGS (Cấu hình đóng băng gắn vào Job lúc Enqueue/Chạy) =====

def list_frozen_configs(workspace_path: Optional[str | Path] = None) -> Dict[str, Any]:
    """Danh sách các snapshot cấu hình đóng băng."""
    return _read_json_safe(_frozen_file_for_workspace(workspace_path), {})


def freeze_job_config(
    video_name: str,
    preset_id: Optional[str] = None,
    overrides: Optional[Dict[str, Any]] = None,
    job_id: Optional[str] = None,
    batch_id: Optional[str] = None,
    workspace_path: Optional[str | Path] = None,
    routing_snapshot: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """
    Tạo snapshot đóng băng cấu hình thực tế cho video/job.
    Snapshot này sẽ không bị thay đổi nếu sau đó preset hay slider toàn cục bị chỉnh sửa.
    """
    # Normalize once so routing/snapshot code never dereferences None.
    overrides = dict(overrides or {})

    # Nếu không truyền preset_id hoặc overrides, thử lấy từ draft đã lưu
    if preset_id is None and not overrides:
        draft = get_video_draft(video_name)
        if draft:
            preset_id = draft.get("preset_id")
            overrides = dict(draft.get("overrides") or {})

    resolved = resolve_effective_config(
        preset_id=preset_id,
        video_overrides=overrides,
    )

    effective = resolved.get("effective_config", {})
    # Presets may filter unknown fields. Retain the complete job-owned policy
    # without persisting credentials or relying on later global settings.
    for key in ("video_mode", "voice_mode", "voice_config", "feature_flags",
                "planned_pipeline", "planned_models", "policy_version", "qc_policy",
                "asr_model", "separation_mode", "script_mode", "visual_sub_to_speech"):
        if key in overrides:
            effective[key] = overrides[key]
    # Keep job-scoped settings tied to the workspace that owns the job.
    config_workspace = Path(workspace_path) if workspace_path else WORKSPACE_DIR
    try:
        from ai.v1_auto_voice import get_auto_voice_config
        auto_cfg = get_auto_voice_config(str(config_workspace))
        if "voice_config" not in effective:
            import voice_selection
            effective["voice_config"] = {**auto_cfg, "manual_voice": dict(voice_selection.selected())}
        if "feature_flags" not in effective:
            from v1_feature_flags import get_feature_flags
            effective["feature_flags"] = get_feature_flags(config_workspace)
        if "dual_voice" not in effective:
            effective["dual_voice"] = bool(auto_cfg.get("dual_voice", False))
        if "voice_mode" not in effective:
            effective["voice_mode"] = "auto" if auto_cfg.get("enabled", True) else "manual"
        if "female_voice_id" not in effective:
            effective["female_voice_id"] = auto_cfg.get("female_voice_id", "capcut-BV562_streaming")
        if "male_voice_id" not in effective:
            effective["male_voice_id"] = auto_cfg.get("male_voice_id", "capcut-BV075_streaming")
    except Exception:
        pass
    planned_pipeline = overrides.get("planned_pipeline") or {}
    planned_models = overrides.get("planned_models") or {}
    planned_asr_model = planned_pipeline.get("asr_model")
    if not planned_asr_model:
        asr_spec = planned_models.get("asr") or {}
        model_id = asr_spec.get("model_id") if isinstance(asr_spec, dict) else None
        if model_id == "qwen3_asr_preview":
            planned_asr_model = "qwen3_asr"
        elif model_id == "whisper_large_v3_turbo":
            planned_asr_model = "whisper_turbo"
        elif model_id == "whisper_sensevoice_hybrid":
            planned_asr_model = "whisper_sensevoice"
        elif model_id == "sensevoice_small":
            planned_asr_model = "sensevoice_small"
    try:
        try:
            from v1_feature_flags import get_feature_flags
        except ImportError:
            from backend.v1_feature_flags import get_feature_flags
        flags = get_feature_flags(config_workspace)
        if "asr_model" not in effective:
            if planned_asr_model:
                effective["asr_model"] = planned_asr_model
            elif flags.get("ENABLE_QWEN_ASR") or flags.get("V1_ASR_MODEL") == "qwen3_asr":
                effective["asr_model"] = "qwen3_asr"
            elif flags.get("V1_ASR_MODEL") == "whisper_turbo":
                effective["asr_model"] = "whisper_turbo"
            elif flags.get("V1_ASR_MODEL") == "sensevoice_small":
                effective["asr_model"] = "sensevoice_small"
            else:
                effective["asr_model"] = "whisper_sensevoice"
    except Exception:
        effective.setdefault("asr_model", "whisper_sensevoice")

    # Tích hợp Video Router Snapshot (Giai đoạn 1)
    router_snapshot = routing_snapshot
    if router_snapshot is None:
        try:
            try:
                from backend.v1_video_router import is_router_enabled, create_router_snapshot
            except ImportError:
                try:
                    from v1_video_router import is_router_enabled, create_router_snapshot
                except ImportError:
                    from .v1_video_router import is_router_enabled, create_router_snapshot
            if is_router_enabled():
                req_mode = effective.get("video_mode") or overrides.get("video_mode", "auto")
                cand_path = video_name
                if not os.path.isabs(str(cand_path)) and not os.path.exists(str(cand_path)):
                    for base in [config_workspace, Path(r"D:\workspace"), Path(r"D:\banve")]:
                        p = base / video_name
                        if p.exists():
                            cand_path = str(p)
                            break
                router_snapshot = create_router_snapshot(
                    file_path=cand_path,
                    requested_mode=req_mode,
                    job_id=job_id,
                    user_overrides=effective,
                    workspace_path=config_workspace,
                )
        except Exception as exc:
            import logging
            logging.getLogger(__name__).warning("Không thể tạo router snapshot cho %s: %s", video_name, exc)
    if router_snapshot:
        effective["video_mode"] = router_snapshot.get("resolved_mode", effective.get("video_mode", "SHORT"))
        effective["planned_pipeline"] = router_snapshot.get("planned_pipeline", effective.get("planned_pipeline", {}))

    snapshot_id = job_id or f"snap-{uuid.uuid4().hex[:8]}"
    now = time.time()

    snapshot = {
        "snapshot_id": snapshot_id,
        "video_name": video_name,
        "job_id": job_id,
        "batch_id": batch_id,
        "preset_id": preset_id,
        "preset_name": resolved.get("preset_name"),
        "overrides": overrides,
        "effective_config": effective,
        "routing": router_snapshot,
        "sources": resolved.get("sources", {}),
        "warnings": resolved.get("warnings", []),
        "revision": 1,
        "created_at": now,
    }

    with _config_transaction(_frozen_file_for_workspace(workspace_path)) as frozen:
        frozen[snapshot_id] = snapshot
        frozen[video_name] = snapshot
        if job_id:
            frozen[job_id] = snapshot
    return snapshot


def resolve_job_frozen_config(
    path_or_key: str,
    workspace_path: Optional[str | Path] = None,
) -> Optional[Dict[str, Any]]:
    """
    Tra cứu snapshot cấu hình đóng băng linh hoạt và thông minh:
    Hỗ trợ tra cứu theo video_name (có/không có extension), job_id, snapshot_id,
    hoặc đường dẫn thư mục/file như out_dir/original.wav, out_dir/mixed.wav.
    """
    if not path_or_key:
        return None
    norm = str(path_or_key).replace("\\", "/")
    base_name = os.path.basename(norm)
    parent_dir = os.path.basename(os.path.dirname(norm)) if "/" in norm else ""

    candidates = set()
    import re
    for item in (base_name, parent_dir, path_or_key):
        if not item or item in ("original.wav", "mixed.wav", "final.ass", "dubbing"):
            continue
        candidates.add(item)
        stripped_batch = re.sub(r"^batch_[a-f0-9]+_", "", item)
        candidates.add(stripped_batch)
        stem = os.path.splitext(item)[0]
        candidates.add(stem)
        stripped_stem = re.sub(r"^batch_[a-f0-9]+_", "", stem)
        candidates.add(stripped_stem)
        for ext in (".mp4", ".mkv", ".mov", ".avi", ".webm", ".flv"):
            candidates.add(f"{stem}{ext}")
            candidates.add(f"{stripped_stem}{ext}")

    for file_path in _frozen_files_for_lookup(path_or_key, workspace_path):
        frozen = _read_json_safe(file_path, {})
        if path_or_key in frozen:
            return frozen[path_or_key]
        for cand in candidates:
            if cand in frozen:
                return frozen[cand]
            for snap in frozen.values():
                if not isinstance(snap, dict):
                    continue
                v_name = snap.get("video_name", "")
                j_id = snap.get("job_id", "")
                s_id = snap.get("snapshot_id", "")
                v_stem = os.path.splitext(v_name)[0] if v_name else ""
                if cand in (v_name, j_id, s_id, v_stem):
                    return snap

    return None


def get_frozen_config(
    key: str,
    workspace_path: Optional[str | Path] = None,
) -> Optional[Dict[str, Any]]:
    """
    Tra cứu snapshot cấu hình đóng băng theo video_name, job_id hoặc snapshot_id.
    Tự động giải quyết thông minh (fuzzy match) theo đường dẫn nếu tra cứu trực tiếp không thấy.
    """
    if not key:
        return None
    return resolve_job_frozen_config(key, workspace_path=workspace_path)


def clear_frozen_config(key: str, workspace_path: Optional[str | Path] = None) -> bool:
    """Xóa snapshot đóng băng sau khi hoàn thành."""
    with _config_transaction(_frozen_file_for_workspace(workspace_path)) as frozen:
        snap = frozen.get(key)
        if not snap:
            return False
        for alias, value in list(frozen.items()):
            if value.get("snapshot_id") == snap.get("snapshot_id"):
                del frozen[alias]
        return True


def update_queued_job_config(
    key: str,
    preset_id: Optional[str] = None,
    overrides: Optional[Dict[str, Any]] = None,
    expected_revision: Optional[int] = None,
    workspace_path: Optional[str | Path] = None,
) -> Dict[str, Any]:
    """
    Cập nhật snapshot cấu hình đóng băng cho một video/job trong hàng chờ.
    Có kiểm tra optimistic locking (expected_revision).
    """
    with _config_transaction(_frozen_file_for_workspace(workspace_path)) as frozen:
        current = frozen.get(key)
        if not current:
            raise KeyError(f"Không tìm thấy cấu hình đóng băng cho '{key}'.")
        if expected_revision is not None and current.get("revision", 1) != expected_revision:
            raise ValueError(f"Xung đột phiên bản: Cấu hình job đã bị sửa (hiện tại: {current.get('revision')}, yêu cầu: {expected_revision}).")
        previous = deepcopy(current.get("effective_config") or {})
        changes = dict(overrides or {})
        # A partial edit must not reload a mutable preset over the frozen job.
        resolved = resolve_effective_config(preset_id=preset_id, video_overrides=changes, system_defaults=previous)
        current["preset_id"] = preset_id if preset_id is not None else current.get("preset_id")
        if preset_id is not None:
            current["preset_name"] = resolved.get("preset_name")
        current["overrides"] = {**current.get("overrides", {}), **changes}
        current["effective_config"] = resolved["effective_config"]
        current["sources"] = {**current.get("sources", {}), **{k: v for k, v in resolved["sources"].items() if v != "system_default"}}
        current["warnings"] = resolved.get("warnings", [])
        current["revision"] = current.get("revision", 1) + 1
        current["updated_at"] = time.time()
        for alias, value in list(frozen.items()):
            if value.get("snapshot_id") == current.get("snapshot_id"):
                frozen[alias] = current
    return current

