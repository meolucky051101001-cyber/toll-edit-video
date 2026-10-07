"""
worker_settings.py - Quản lý cấu hình số luồng xử lý (Concurrency) và bộ bảo vệ chống tràn VRAM cho Tool V1.
Lưu bền vững cấu hình vào workspace/bot_system/worker_settings.json.
- concurrency: 1 (Mặc định / An toàn tuyệt đối) | 2 (So le thông minh với GPU Gatekeeper)
- auto_vram_guard: bool (Tự động hãm luồng nếu VRAM quá tải)
- max_vram_percent_threshold: float (Ngưỡng VRAM cao nhất để kích hoạt hạ luồng, mặc định 85.0%)
- min_free_vram_mb: float (Mức VRAM trống tối thiểu để duy trì 2 luồng, mặc định 1500.0MB)
- auto_throttled: bool (Cờ đánh dấu hệ thống vừa tự động bỏ 1 luồng để bảo vệ VRAM)
- throttle_reason: str (Lý do hạ luồng)
"""

from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path
from typing import Any, Dict

logger = logging.getLogger("worker_settings")

ROOT = Path(__file__).resolve().parent
WORKSPACE = Path(os.getenv("AUTODUB_WORKSPACE", str(ROOT.parent / "workspace")))

def _resolve_settings_file() -> Path:
    bot_system = WORKSPACE / "bot_system"
    target = bot_system / "worker_settings.json"
    if target.exists() or bot_system.is_dir():
        return target
    return WORKSPACE / "worker_settings.json"

SETTINGS_FILE = _resolve_settings_file()

DEFAULT_WORKER_SETTINGS: Dict[str, Any] = {
    "concurrency": 1,               # 1 hoặc 2 (cấu hình người dùng mong muốn)
    "auto_vram_guard": True,        # Tự động bảo vệ chống tràn VRAM
    "max_vram_percent_threshold": 85.0,  # Ngưỡng VRAM cao nhất kích hoạt hạ luồng
    "min_free_vram_mb": 1500.0,     # Mức VRAM trống tối thiểu (MB)
    "auto_throttled": False,        # True nếu đang bị tự động hạ về 1 luồng do VRAM cao
    "throttle_reason": "",
    "throttled_at": 0.0,
}


def get_worker_settings() -> Dict[str, Any]:
    """Đọc cấu hình số luồng và trạng thái bảo vệ VRAM hiện tại."""
    try:
        if SETTINGS_FILE.is_file():
            data = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
            concurrency = int(data.get("concurrency", 1))
            if concurrency not in (1, 2):
                concurrency = 1
            auto_guard = bool(data.get("auto_vram_guard", True))
            max_pct = float(data.get("max_vram_percent_threshold", 85.0))
            min_free = float(data.get("min_free_vram_mb", 1500.0))
            throttled = bool(data.get("auto_throttled", False))
            reason = str(data.get("throttle_reason", ""))
            t_at = float(data.get("throttled_at", 0.0))

            # Số luồng hiệu lực thực tế đang chạy
            effective = 1 if (throttled or concurrency == 1) else 2

            return {
                "concurrency": concurrency,
                "effective_concurrency": effective,
                "auto_vram_guard": auto_guard,
                "max_vram_percent_threshold": max_pct,
                "min_free_vram_mb": min_free,
                "auto_throttled": throttled,
                "throttle_reason": reason,
                "throttled_at": t_at,
            }
    except Exception as e:
        logger.warning("Không thể đọc worker_settings.json: %s. Dùng mặc định.", e)

    fallback = dict(DEFAULT_WORKER_SETTINGS)
    fallback["effective_concurrency"] = 1
    return fallback


def get_effective_concurrency() -> int:
    """Trả về số luồng hiệu lực thực tế (1 hoặc 2)."""
    cfg = get_worker_settings()
    return cfg.get("effective_concurrency", 1)


def save_worker_settings(
    concurrency: int,
    auto_vram_guard: bool = True,
    max_vram_percent_threshold: float = 85.0,
    min_free_vram_mb: float = 1500.0,
    reset_throttle: bool = True,
) -> Dict[str, Any]:
    """Lưu cấu hình số luồng vào file bền vững."""
    if concurrency not in (1, 2):
        raise ValueError(
            f"Số luồng không hợp lệ: {concurrency}. "
            "Trên card RTX 4050 6GB chỉ hỗ trợ 1 luồng hoặc 2 luồng so le an toàn."
        )

    clean_cfg = {
        "concurrency": int(concurrency),
        "auto_vram_guard": bool(auto_vram_guard),
        "max_vram_percent_threshold": round(float(max_vram_percent_threshold), 1),
        "min_free_vram_mb": round(float(min_free_vram_mb), 1),
        "auto_throttled": False if reset_throttle else get_worker_settings().get("auto_throttled", False),
        "throttle_reason": "" if reset_throttle else get_worker_settings().get("throttle_reason", ""),
        "throttled_at": 0.0 if reset_throttle else get_worker_settings().get("throttled_at", 0.0),
    }

    try:
        SETTINGS_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp_file = SETTINGS_FILE.with_suffix(".tmp")
        tmp_file.write_text(json.dumps(clean_cfg, indent=2, ensure_ascii=False), encoding="utf-8")
        tmp_file.replace(SETTINGS_FILE)
        logger.info("Đã lưu worker_settings: concurrency=%s (throttled=%s)", concurrency, clean_cfg["auto_throttled"])
    except Exception as e:
        logger.error("Lỗi khi lưu worker_settings.json: %s", e)
        raise

    clean_cfg["effective_concurrency"] = 1 if clean_cfg["auto_throttled"] else clean_cfg["concurrency"]
    return clean_cfg


def trigger_vram_emergency_downgrade(reason: str) -> bool:
    """
    CƠ CHẾ AN TOÀN KHẨN CẤP: TỰ ĐỘNG BỎ 1 LUỒNG KHI VRAM SẮP TRÀN!
    Khi VRAM chạm ngưỡng nguy hiểm (>85% hoặc trống < 1500MB):
    Lập tức hạ effective_concurrency về 1 luồng duy nhất để bảo vệ GPU chống crash.
    """
    cfg = get_worker_settings()
    if not cfg.get("auto_vram_guard", True):
        logger.info("[VRAM_GUARD] auto_vram_guard đang tắt, không hạ luồng.")
        return False

    if cfg.get("concurrency", 1) <= 1:
        # Đang là 1 luồng sẵn rồi, không cần hạ thêm
        return False

    if cfg.get("auto_throttled", False):
        # Đã bị hạ rồi
        return True

    logger.warning(
        "[VRAM_GUARD] 🚨 KÍCH HOẠT CƠ CHẾ AN TOÀN: VRAM SẮP TRÀN! Tự động bỏ 1 luồng: %s",
        reason,
    )

    clean_cfg = {
        "concurrency": int(cfg.get("concurrency", 2)),
        "auto_vram_guard": True,
        "max_vram_percent_threshold": float(cfg.get("max_vram_percent_threshold", 85.0)),
        "min_free_vram_mb": float(cfg.get("min_free_vram_mb", 1500.0)),
        "auto_throttled": True,
        "throttle_reason": reason,
        "throttled_at": time.time(),
    }

    try:
        SETTINGS_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp_file = SETTINGS_FILE.with_suffix(".tmp")
        tmp_file.write_text(json.dumps(clean_cfg, indent=2, ensure_ascii=False), encoding="utf-8")
        tmp_file.replace(SETTINGS_FILE)
        return True
    except Exception as e:
        logger.error("Lỗi khi ghi sự kiện hạ luồng VRAM: %s", e)
        return False


def reset_vram_throttle() -> bool:
    """Khôi phục lại số luồng ban đầu khi VRAM đã hạ nhiệt an toàn."""
    cfg = get_worker_settings()
    if not cfg.get("auto_throttled", False):
        return True

    logger.info("[VRAM_GUARD] ✅ VRAM đã an toàn trở lại. Khôi phục số luồng về %s.", cfg.get("concurrency", 1))
    clean_cfg = {
        "concurrency": int(cfg.get("concurrency", 1)),
        "auto_vram_guard": True,
        "max_vram_percent_threshold": float(cfg.get("max_vram_percent_threshold", 85.0)),
        "min_free_vram_mb": float(cfg.get("min_free_vram_mb", 1500.0)),
        "auto_throttled": False,
        "throttle_reason": "",
        "throttled_at": 0.0,
    }

    try:
        SETTINGS_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp_file = SETTINGS_FILE.with_suffix(".tmp")
        tmp_file.write_text(json.dumps(clean_cfg, indent=2, ensure_ascii=False), encoding="utf-8")
        tmp_file.replace(SETTINGS_FILE)
        return True
    except Exception as e:
        logger.error("Lỗi khi khôi phục số luồng VRAM: %s", e)
        return False
