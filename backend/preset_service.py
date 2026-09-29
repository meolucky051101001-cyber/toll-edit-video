"""
Preset Service - Quản lý và xử lý cấu hình Preset cho Tool V1 và Tool V2.
Tuân thủ tiêu chuẩn:
- Schema: id, name, schema_version, revision, tool_compatibility, config, created_at, updated_at
- Atomic file write với temp file và rename
- Optimistic locking qua revision
- Resolve config: Mặc định hệ thống -> Preset -> Cấu hình riêng video -> Effective Config
"""

import os
import sys
import json
import time
import uuid
import copy
from pathlib import Path
from typing import Dict, Any, List, Optional, Tuple

try:
    from backend.config.paths import AppPaths
except ImportError:
    from config.paths import AppPaths

ROOT_DIR = Path(__file__).resolve().parent
WORKSPACE_DIR = AppPaths.from_environment(ROOT_DIR.parent).workspace
CONTROL_DIR = WORKSPACE_DIR / "control"
CONTROL_DIR.mkdir(parents=True, exist_ok=True)

PRESETS_FILE = CONTROL_DIR / "presets.json"

SCHEMA_VERSION = "1.0.0"

# Hệ thống mặc định toàn cục
SYSTEM_DEFAULTS = {
    "voice_id": "microsoft-hoaimy",
    "speed": 1.0,
    "bgm_volume_db": -2.0,
    "dubbing_volume_db": 1.0,
    "language": "vi",
    "subtitle_style": "default",
}

DEFAULT_PRESETS = [
    {
        "id": "preset-review-sp",
        "name": "Review sản phẩm",
        "schema_version": SCHEMA_VERSION,
        "revision": 1,
        "tool_compatibility": "both",
        "config": {
            "voice_id": "microsoft-hoaimy",
            "speed": 1.05,
            "bgm_volume_db": -3.0,
            "dubbing_volume_db": 2.0,
            "language": "vi",
            "subtitle_style": "modern_yellow",
        },
        "description": "Nhịp nhanh, giọng rõ ràng, nhạc nền lùi sâu tôn giọng dubbing.",
        "created_at": 1727500000.0,
        "updated_at": 1727500000.0,
    },
    {
        "id": "preset-huong-dan",
        "name": "Video hướng dẫn",
        "schema_version": SCHEMA_VERSION,
        "revision": 1,
        "tool_compatibility": "both",
        "config": {
            "voice_id": "microsoft-namminh",
            "speed": 1.0,
            "bgm_volume_db": -5.0,
            "dubbing_volume_db": 1.0,
            "language": "vi",
            "subtitle_style": "clear_white",
        },
        "description": "Giọng nam trầm ấm, phát âm rành mạch, nhạc nhẹ nền tảng.",
        "created_at": 1727500000.0,
        "updated_at": 1727500000.0,
    },
    {
        "id": "preset-ke-chuyen",
        "name": "Kể chuyện / Tâm sự",
        "schema_version": SCHEMA_VERSION,
        "revision": 1,
        "tool_compatibility": "both",
        "config": {
            "voice_id": "chi-mai",
            "speed": 0.95,
            "bgm_volume_db": -1.0,
            "dubbing_volume_db": 1.5,
            "language": "vi",
            "subtitle_style": "story_italic",
        },
        "description": "Tốc độ chậm rãi, biểu cảm, BGM hoà quyện giàu cảm xúc.",
        "created_at": 1727500000.0,
        "updated_at": 1727500000.0,
    },
]


def _atomic_write_json(file_path: Path, data: Any):
    file_path.parent.mkdir(parents=True, exist_ok=True)
    temp_file = file_path.with_name(f"{file_path.name}.tmp.{uuid.uuid4().hex[:8]}")
    with open(temp_file, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(temp_file, file_path)


def init_presets_storage() -> List[Dict[str, Any]]:
    """Khởi tạo kho lưu trữ preset với 3 preset mặc định nếu chưa có."""
    if not PRESETS_FILE.exists():
        _atomic_write_json(PRESETS_FILE, DEFAULT_PRESETS)
        return copy.deepcopy(DEFAULT_PRESETS)
    try:
        data = json.loads(PRESETS_FILE.read_text(encoding="utf-8"))
        if not isinstance(data, list):
            data = DEFAULT_PRESETS
            _atomic_write_json(PRESETS_FILE, data)
        return data
    except Exception:
        _atomic_write_json(PRESETS_FILE, DEFAULT_PRESETS)
        return copy.deepcopy(DEFAULT_PRESETS)


def list_presets(tool: str = "both") -> List[Dict[str, Any]]:
    """Liệt kê danh sách các preset, có thể lọc theo phiên bản tool (v1, v2, both)."""
    presets = init_presets_storage()
    if tool == "both":
        return presets
    return [
        p for p in presets
        if p.get("tool_compatibility") in ("both", tool)
    ]


def get_preset(preset_id: str) -> Optional[Dict[str, Any]]:
    """Lấy chi tiết 1 preset theo ID."""
    presets = init_presets_storage()
    for p in presets:
        if p.get("id") == preset_id:
            return copy.deepcopy(p)
    return None


def create_preset(data: Dict[str, Any]) -> Dict[str, Any]:
    """Tạo mới một preset với schema chuẩn."""
    name = (data.get("name") or "").strip()
    if not name:
        raise ValueError("Tên preset không được để trống.")

    config_raw = data.get("config", {})
    # Chuẩn hoá config
    config = {
        "voice_id": str(config_raw.get("voice_id", SYSTEM_DEFAULTS["voice_id"])),
        "speed": float(config_raw.get("speed", SYSTEM_DEFAULTS["speed"])),
        "bgm_volume_db": round(float(config_raw.get("bgm_volume_db", SYSTEM_DEFAULTS["bgm_volume_db"])), 1),
        "dubbing_volume_db": round(float(config_raw.get("dubbing_volume_db", SYSTEM_DEFAULTS["dubbing_volume_db"])), 1),
        "language": str(config_raw.get("language", SYSTEM_DEFAULTS["language"])),
        "subtitle_style": str(config_raw.get("subtitle_style", SYSTEM_DEFAULTS["subtitle_style"])),
    }

    # Giới hạn an toàn
    config["speed"] = max(0.5, min(2.0, config["speed"]))
    config["bgm_volume_db"] = max(-40.0, min(10.0, config["bgm_volume_db"]))
    config["dubbing_volume_db"] = max(-20.0, min(15.0, config["dubbing_volume_db"]))

    preset_id = data.get("id") or f"preset-{uuid.uuid4().hex[:8]}"
    now = time.time()
    
    new_preset = {
        "id": preset_id,
        "name": name,
        "schema_version": SCHEMA_VERSION,
        "revision": 1,
        "tool_compatibility": data.get("tool_compatibility", "both"),
        "config": config,
        "description": data.get("description", "").strip(),
        "created_at": now,
        "updated_at": now,
    }

    presets = init_presets_storage()
    if any(p.get("id") == preset_id for p in presets):
        raise ValueError(f"Preset ID '{preset_id}' đã tồn tại.")

    presets.append(new_preset)
    _atomic_write_json(PRESETS_FILE, presets)
    return new_preset


def update_preset(preset_id: str, data: Dict[str, Any], expected_revision: Optional[int] = None) -> Dict[str, Any]:
    """Cập nhật một preset với kiểm tra optimistic locking (revision)."""
    presets = init_presets_storage()
    target_idx = None
    for idx, p in enumerate(presets):
        if p.get("id") == preset_id:
            target_idx = idx
            break

    if target_idx is None:
        raise KeyError(f"Không tìm thấy preset ID '{preset_id}'.")

    current = presets[target_idx]
    if expected_revision is not None and current.get("revision", 1) != expected_revision:
        raise ValueError(
            f"Xung đột phiên bản: Preset đã bị chỉnh sửa (phiên bản hiện tại: {current.get('revision')}, yêu cầu: {expected_revision})."
        )

    if "name" in data and data["name"].strip():
        current["name"] = data["name"].strip()
    if "description" in data:
        current["description"] = data["description"].strip()
    if "tool_compatibility" in data:
        current["tool_compatibility"] = data["tool_compatibility"]

    if "config" in data and isinstance(data["config"], dict):
        cfg = current.setdefault("config", {})
        for k in ("voice_id", "language", "subtitle_style"):
            if k in data["config"]:
                cfg[k] = str(data["config"][k])
        if "speed" in data["config"]:
            cfg["speed"] = max(0.5, min(2.0, float(data["config"]["speed"])))
        if "bgm_volume_db" in data["config"]:
            cfg["bgm_volume_db"] = round(max(-40.0, min(10.0, float(data["config"]["bgm_volume_db"]))), 1)
        if "dubbing_volume_db" in data["config"]:
            cfg["dubbing_volume_db"] = round(max(-20.0, min(15.0, float(data["config"]["dubbing_volume_db"]))), 1)

    current["revision"] = current.get("revision", 1) + 1
    current["updated_at"] = time.time()
    presets[target_idx] = current
    _atomic_write_json(PRESETS_FILE, presets)
    return current


def delete_preset(preset_id: str, expected_revision: Optional[int] = None) -> bool:
    """Xóa 1 preset."""
    # Bảo vệ preset mặc định cốt lõi không bị xoá sạch nếu còn lại ít hơn 1
    presets = init_presets_storage()
    target = None
    for p in presets:
        if p.get("id") == preset_id:
            target = p
            break
    if not target:
        raise KeyError(f"Không tìm thấy preset ID '{preset_id}'.")

    if expected_revision is not None and target.get("revision", 1) != expected_revision:
        raise ValueError(f"Xung đột phiên bản khi xóa preset {preset_id}.")

    new_presets = [p for p in presets if p.get("id") != preset_id]
    _atomic_write_json(PRESETS_FILE, new_presets)
    return True


def resolve_effective_config(
    preset_id: Optional[str] = None,
    video_overrides: Optional[Dict[str, Any]] = None,
    system_defaults: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """
    Resolve cấu hình thực tế theo thứ tự nghiêm ngặt:
    Mặc định hệ thống -> Preset được chọn -> Ghi đè riêng của video
    
    Trả về:
    {
        "effective_config": dict,
        "sources": { key: "system_default" | "preset" | "video_override" },
        "warnings": list of str,
        "preset_name": str or None,
    }
    """
    base_defaults = copy.deepcopy(SYSTEM_DEFAULTS)
    if system_defaults:
        base_defaults.update(system_defaults)

    effective = copy.deepcopy(base_defaults)
    sources = {k: "system_default" for k in base_defaults}
    warnings = []
    preset_name = None

    # Áp dụng Preset nếu có
    if preset_id:
        preset = get_preset(preset_id)
        if preset:
            preset_name = preset.get("name")
            p_cfg = preset.get("config", {})
            for k, v in p_cfg.items():
                if v is not None:
                    effective[k] = v
                    sources[k] = f"preset ({preset_name})"
        else:
            warnings.append(f"Preset '{preset_id}' không tồn tại; đã dùng mặc định hệ thống.")

    # Áp dụng Cấu hình riêng video (Overrides) nếu có
    if video_overrides:
        for k, v in video_overrides.items():
            if v is not None and v != "":
                # Kiểm tra hợp lệ kiểu dữ liệu
                if k in ("speed", "bgm_volume_db", "dubbing_volume_db"):
                    try:
                        v = float(v)
                        if k == "speed":
                            v = max(0.5, min(2.0, v))
                        elif k == "bgm_volume_db":
                            v = round(max(-40.0, min(10.0, v)), 1)
                        elif k == "dubbing_volume_db":
                            v = round(max(-20.0, min(15.0, v)), 1)
                    except (ValueError, TypeError):
                        warnings.append(f"Giá trị '{v}' cho '{k}' không hợp lệ; bỏ qua ghi đè.")
                        continue
                effective[k] = v
                sources[k] = "video_override"

    # Kiểm tra tính tương thích của giọng
    from voice_selection import catalog
    try:
        cat = catalog()
        valid_voice_ids = {item["id"] for item in cat}
        if effective.get("voice_id") not in valid_voice_ids:
            warnings.append(
                f"Giọng '{effective.get('voice_id')}' không có trong danh mục hệ thống. Khi chạy có thể sẽ tự fallback về giọng mặc định."
            )
    except Exception:
        pass

    return {
        "effective_config": effective,
        "sources": sources,
        "warnings": warnings,
        "preset_id": preset_id,
        "preset_name": preset_name,
    }
