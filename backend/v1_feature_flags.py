"""
v1_feature_flags.py - Quản lý cấu hình cờ tính năng bền vững xuyên tiến trình cho Tool V1.
Lưu trữ trên đĩa tại workspace/bot_system/v1_feature_flags.json để cả Dashboard, Telegram Bot và Batch Worker
luôn đọc đồng nhất một nguồn sự thật, không chỉ dựa vào os.environ cục bộ của một tiến trình.
"""

from __future__ import annotations

import os
import json
import logging
import uuid
import math
from pathlib import Path
from typing import Any, Dict, Optional

try:
    from .v1_speech_guard import natural_speed_limit
except ImportError:
    from v1_speech_guard import natural_speed_limit

logger = logging.getLogger(__name__)

DEFAULT_FLAGS: Dict[str, Any] = {
    "V1_ASR_MODEL": "whisper_sensevoice",
    "V1_ASR_CHUNKING": True,
    "V1_SMART_SKIP_OCR": True,
    "V1_QC_POLICY": "BLOCK",
    "V1_MAX_NATURAL_SPEED": 1.50,
    "V1_TTS_WORKERS": 6,
    "V1_USE_ORCHESTRATOR": True,
    "V1_VIDEO_MODE": "AUTO",
    "V1_SHORT_SEPARATOR": "roformer",
    "V1_LONG_SEPARATOR": "demucs",
    "V1_SHORT_ASR_MODEL": "inherit",
    "V1_LONG_ASR_MODEL": "inherit",
    "V1_VISUAL_SUB_TO_SPEECH": False,
}


# Legacy ASR values -> current model. The single place allowed to know the
# retired model id; every reader funnels old config through here.
LEGACY_ASR_MODEL_ALIASES = {
    "qwen3_asr": "whisper_sensevoice",
    "qwen3_asr_preview": "whisper_sensevoice",
    "qwen": "whisper_sensevoice",
    "qwen3": "whisper_sensevoice",
}
LEGACY_ASR_FLAG_KEYS = ("ENABLE_QWEN_ASR",)
_legacy_asr_warned = False


def _warn_legacy_asr(source: str, legacy_value: str, replacement: str) -> None:
    global _legacy_asr_warned
    if _legacy_asr_warned:
        return
    logger.warning(
        "ASR model %r đã ngừng hỗ trợ%s; tự động chuyển sang %s.",
        legacy_value,
        f" ({source})" if source else "",
        replacement,
    )
    _legacy_asr_warned = True


def migrate_legacy_asr_model(value: Any, source: str = "") -> Any:
    """Map retired ASR model ids onto whisper_sensevoice, logging a single warning."""
    if not isinstance(value, str):
        return value
    replacement = LEGACY_ASR_MODEL_ALIASES.get(value.strip().lower())
    if replacement is None:
        return value
    _warn_legacy_asr(source, value, replacement)
    return replacement


def _drop_legacy_asr_flag(flags: Dict[str, Any]) -> bool:
    """Remove the retired toggle; return True when it used to be enabled."""
    enabled = False
    for key in LEGACY_ASR_FLAG_KEYS:
        if flags.pop(key, None) is True:
            enabled = True
    return enabled


def _get_flags_file(workspace_path: Optional[str | Path] = None) -> Path:
    if workspace_path:
        base = Path(workspace_path)
    else:
        base = Path(os.getenv("AUTODUB_WORKSPACE", str(Path(__file__).resolve().parent.parent / "workspace")))
    target = base / "bot_system" / "v1_feature_flags.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    return target


def get_feature_flags(workspace_path: Optional[str | Path] = None) -> Dict[str, Any]:
    """Đọc cờ tính năng từ file cấu hình bền vững, kết hợp fallback mặc định."""
    target = _get_flags_file(workspace_path)
    flags = dict(DEFAULT_FLAGS)
    if target.is_file():
        try:
            data = json.loads(target.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                flags.update(data)
        except Exception as exc:
            logger.warning("Không thể đọc v1_feature_flags.json: %s", exc)
    flags["V1_MAX_NATURAL_SPEED"] = natural_speed_limit(flags["V1_MAX_NATURAL_SPEED"])
    if _drop_legacy_asr_flag(flags):
        _warn_legacy_asr("feature flags", LEGACY_ASR_FLAG_KEYS[0], "whisper_sensevoice")
        flags["V1_ASR_MODEL"] = "whisper_sensevoice"
    flags["V1_ASR_MODEL"] = migrate_legacy_asr_model(flags.get("V1_ASR_MODEL"), "feature flags")
    return flags


def set_feature_flags(new_flags: Dict[str, Any], workspace_path: Optional[str | Path] = None) -> Dict[str, Any]:
    """Validate and atomically persist settings for new jobs, not active jobs."""
    target = _get_flags_file(workspace_path)
    current = get_feature_flags(workspace_path)
    updates = dict(new_flags)
    if _drop_legacy_asr_flag(updates):
        _warn_legacy_asr("settings", LEGACY_ASR_FLAG_KEYS[0], "whisper_sensevoice")
        updates.setdefault("V1_ASR_MODEL", "whisper_sensevoice")
    for asr_key in ("V1_ASR_MODEL", "V1_SHORT_ASR_MODEL", "V1_LONG_ASR_MODEL"):
        if asr_key in updates:
            updates[asr_key] = migrate_legacy_asr_model(updates[asr_key], "settings")
    if "V1_ASR_MODEL" in updates:
        m = str(updates["V1_ASR_MODEL"]).strip().lower()
        if m in ("whisper_sensevoice", "whisper_sensevoice_hybrid", "hybrid"):
            updates["V1_ASR_MODEL"] = "whisper_sensevoice"
        elif m in ("sensevoice_small", "sensevoice"):
            updates["V1_ASR_MODEL"] = "sensevoice_small"
        elif m in ("whisper_turbo", "whisper"):
            updates["V1_ASR_MODEL"] = "whisper_turbo"
    for key, value in updates.items():
        if key not in DEFAULT_FLAGS:
            raise ValueError(f"Unknown V1 setting: {key}")
        if isinstance(DEFAULT_FLAGS[key], bool) and type(value) is not bool:
            raise ValueError(f"{key} requires a boolean")
    choices = {
        "V1_VIDEO_MODE": {"AUTO", "SHORT", "MEDIUM", "LONG", "CUSTOM"},
        "V1_ASR_MODEL": {"whisper_turbo", "whisper_sensevoice", "sensevoice_small"},
        "V1_SHORT_ASR_MODEL": {"inherit", "whisper_turbo", "whisper_sensevoice", "sensevoice_small"},
        "V1_LONG_ASR_MODEL": {"inherit", "whisper_turbo", "whisper_sensevoice", "sensevoice_small"},
        "V1_SHORT_SEPARATOR": {"roformer", "demucs"},
        "V1_LONG_SEPARATOR": {"roformer", "demucs"},
        "V1_QC_POLICY": {"REPORT_ONLY", "WARN", "BLOCK"},
    }
    for key, allowed in choices.items():
        if key in updates and updates[key] not in allowed:
            raise ValueError(f"Invalid {key}: {updates[key]}")
    if "V1_TTS_WORKERS" in updates:
        value = updates["V1_TTS_WORKERS"]
        if type(value) is not int or not 1 <= value <= 6:
            raise ValueError("V1_TTS_WORKERS must be an integer from 1 to 6")
    if "V1_MAX_NATURAL_SPEED" in updates:
        value = updates["V1_MAX_NATURAL_SPEED"]
        if type(value) not in (int, float) or not math.isfinite(value) or not 1.00 <= value <= 1.50:
            raise ValueError("V1_MAX_NATURAL_SPEED must be between 1.00 and 1.50")

    for k, v in updates.items():
        if k in DEFAULT_FLAGS:
            current[k] = v

    temp_file = target.with_name(f"{target.name}.tmp.{uuid.uuid4().hex}")
    try:
        temp_file.write_text(json.dumps(current, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(temp_file, target)
    except Exception as exc:
        logger.error("Lỗi lưu v1_feature_flags.json: %s", exc)
        raise RuntimeError(f"Không thể lưu feature flags vào {target}: {exc}") from exc
    finally:
        try:
            temp_file.unlink()
        except OSError:
            pass

    # Active jobs consume snapshots; a settings change applies only to new jobs.
    return current


def sync_env_from_flags(workspace_path: Optional[str | Path] = None) -> Dict[str, Any]:
    """Đồng bộ os.environ từ file cấu hình bền vững khi tiến trình khởi động hoặc bắt đầu job."""
    flags = get_feature_flags(workspace_path)
    for k, v in flags.items():
        if isinstance(v, bool):
            os.environ[k] = "true" if v else "false"
        else:
            os.environ[k] = str(v)
    return flags
