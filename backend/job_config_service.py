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
from pathlib import Path
from typing import Dict, Any, List, Optional

try:
    from backend.config.paths import AppPaths
except ImportError:
    from config.paths import AppPaths

ROOT_DIR = Path(__file__).resolve().parent
WORKSPACE_DIR = AppPaths.from_environment(ROOT_DIR.parent).workspace
CONTROL_DIR = WORKSPACE_DIR / "control"
CONTROL_DIR.mkdir(parents=True, exist_ok=True)

DRAFTS_FILE = CONTROL_DIR / "video_draft_configs.json"
FROZEN_FILE = CONTROL_DIR / "job_frozen_configs.json"

# Idempotency cache (key -> {result, created_at})
_IDEMPOTENCY_CACHE: Dict[str, Dict[str, Any]] = {}
_IDEMPOTENCY_TTL_SEC = 300.0  # 5 minutes


def check_idempotency(key: str) -> Optional[Dict[str, Any]]:
    now = time.time()
    if key in _IDEMPOTENCY_CACHE:
        entry = _IDEMPOTENCY_CACHE[key]
        if now - entry.get("created_at", 0) < _IDEMPOTENCY_TTL_SEC:
            return entry.get("result")
        else:
            del _IDEMPOTENCY_CACHE[key]
    return None


def record_idempotency(key: str, result: Dict[str, Any]):
    now = time.time()
    # Prune expired keys
    expired = [k for k, v in _IDEMPOTENCY_CACHE.items() if now - v.get("created_at", 0) >= _IDEMPOTENCY_TTL_SEC]
    for k in expired:
        _IDEMPOTENCY_CACHE.pop(k, None)
    _IDEMPOTENCY_CACHE[key] = {
        "created_at": now,
        "result": result,
    }


try:
    from preset_service import resolve_effective_config, get_preset
except ImportError:
    from .preset_service import resolve_effective_config, get_preset


def _atomic_write_json(file_path: Path, data: Any):
    file_path.parent.mkdir(parents=True, exist_ok=True)
    temp_file = file_path.with_name(f"{file_path.name}.tmp.{uuid.uuid4().hex[:8]}")
    with open(temp_file, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(temp_file, file_path)


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
    drafts = list_all_drafts()
    now = time.time()
    
    current = drafts.get(video_name, {
        "video_name": video_name,
        "created_at": now,
        "revision": 0,
    })
    
    current["preset_id"] = draft_data.get("preset_id")
    current["overrides"] = draft_data.get("overrides", {})
    current["revision"] = current.get("revision", 0) + 1
    current["updated_at"] = now

    drafts[video_name] = current
    _atomic_write_json(DRAFTS_FILE, drafts)
    return current


def delete_video_draft(video_name: str) -> bool:
    """Xóa bản nháp cấu hình riêng của video."""
    drafts = list_all_drafts()
    if video_name in drafts:
        del drafts[video_name]
        _atomic_write_json(DRAFTS_FILE, drafts)
        return True
    return False


# ===== FROZEN CONFIGS (Cấu hình đóng băng gắn vào Job lúc Enqueue/Chạy) =====

def list_frozen_configs() -> Dict[str, Any]:
    """Danh sách các snapshot cấu hình đóng băng."""
    return _read_json_safe(FROZEN_FILE, {})


def freeze_job_config(
    video_name: str,
    preset_id: Optional[str] = None,
    overrides: Optional[Dict[str, Any]] = None,
    job_id: Optional[str] = None,
    batch_id: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Tạo snapshot đóng băng cấu hình thực tế cho video/job.
    Snapshot này sẽ không bị thay đổi nếu sau đó preset hay slider toàn cục bị chỉnh sửa.
    """
    # Nếu không truyền preset_id hoặc overrides, thử lấy từ draft đã lưu
    if preset_id is None and overrides is None:
        draft = get_video_draft(video_name)
        if draft:
            preset_id = draft.get("preset_id")
            overrides = draft.get("overrides")

    resolved = resolve_effective_config(
        preset_id=preset_id,
        video_overrides=overrides,
    )

    snapshot_id = job_id or f"snap-{uuid.uuid4().hex[:8]}"
    now = time.time()

    snapshot = {
        "snapshot_id": snapshot_id,
        "video_name": video_name,
        "job_id": job_id,
        "batch_id": batch_id,
        "preset_id": preset_id,
        "preset_name": resolved.get("preset_name"),
        "effective_config": resolved.get("effective_config", {}),
        "sources": resolved.get("sources", {}),
        "warnings": resolved.get("warnings", []),
        "revision": 1,
        "created_at": now,
    }

    frozen = list_frozen_configs()
    # Lưu theo cả snapshot_id và video_name để worker dễ tra cứu
    frozen[snapshot_id] = snapshot
    frozen[video_name] = snapshot
    if job_id:
        frozen[job_id] = snapshot

    _atomic_write_json(FROZEN_FILE, frozen)
    return snapshot


def get_frozen_config(key: str) -> Optional[Dict[str, Any]]:
    """
    Tra cứu snapshot cấu hình đóng băng theo video_name, job_id hoặc snapshot_id.
    """
    frozen = list_frozen_configs()
    return frozen.get(key)


def clear_frozen_config(key: str) -> bool:
    """Xóa snapshot đóng băng sau khi hoàn thành."""
    frozen = list_frozen_configs()
    found = False
    snap = frozen.get(key)
    if snap:
        found = True
        snap_id = snap.get("snapshot_id")
        v_name = snap.get("video_name")
        j_id = snap.get("job_id")
        for k in (snap_id, v_name, j_id, key):
            if k and k in frozen:
                del frozen[k]
        _atomic_write_json(FROZEN_FILE, frozen)
    return found


def update_queued_job_config(
    key: str,
    preset_id: Optional[str] = None,
    overrides: Optional[Dict[str, Any]] = None,
    expected_revision: Optional[int] = None,
) -> Dict[str, Any]:
    """
    Cập nhật snapshot cấu hình đóng băng cho một video/job trong hàng chờ.
    Có kiểm tra optimistic locking (expected_revision).
    """
    frozen = list_frozen_configs()
    current = frozen.get(key)
    if not current:
        raise KeyError(f"Không tìm thấy cấu hình đóng băng cho '{key}'.")

    if expected_revision is not None and current.get("revision", 1) != expected_revision:
        raise ValueError(
            f"Xung đột phiên bản: Cấu hình job đã bị sửa (hiện tại: {current.get('revision')}, yêu cầu: {expected_revision})."
        )

    resolved = resolve_effective_config(
        preset_id=preset_id if preset_id is not None else current.get("preset_id"),
        video_overrides=overrides if overrides is not None else current.get("overrides"),
    )

    current["preset_id"] = preset_id if preset_id is not None else current.get("preset_id")
    current["preset_name"] = resolved.get("preset_name")
    current["effective_config"] = resolved.get("effective_config", {})
    current["sources"] = resolved.get("sources", {})
    current["warnings"] = resolved.get("warnings", [])
    current["revision"] = current.get("revision", 1) + 1
    current["updated_at"] = time.time()

    # Cập nhật mọi alias của snapshot
    snap_id = current.get("snapshot_id")
    v_name = current.get("video_name")
    j_id = current.get("job_id")
    for k in (snap_id, v_name, j_id, key):
        if k:
            frozen[k] = current

    _atomic_write_json(FROZEN_FILE, frozen)
    return current

