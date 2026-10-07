"""
v1_vram_monitor.py - Trình giám sát VRAM & GPU NVIDIA thời gian thực cho Tool V1.
Sử dụng trực tiếp thư viện Ctypes gọi nvml.dll (chuẩn driver NVIDIA Windows).
- Tốc độ siêu nhanh (< 1ms), không tốn CPU/RAM, không cần cài thêm gói thư viện ngoài.
- Theo dõi chính xác dung lượng VRAM đã dùng, còn trống, nhiệt độ, và % tải GPU.
- Cung cấp cơ chế cảnh báo ngưỡng và đánh giá độ an toàn trước khi chạy các bước AI nặng.
"""

from __future__ import annotations

import ctypes
import logging
import os
import time
from typing import Any, Dict, Optional, Tuple

logger = logging.getLogger("v1_vram_monitor")

# Cấu trúc dữ liệu Memory & Utilization từ NVML
class _NvmlMemory(ctypes.Structure):
    _fields_ = [
        ("total", ctypes.c_ulonglong),
        ("free", ctypes.c_ulonglong),
        ("used", ctypes.c_ulonglong),
    ]

class _NvmlUtilization(ctypes.Structure):
    _fields_ = [
        ("gpu", ctypes.c_uint),
        ("memory", ctypes.c_uint),
    ]

# Cache bộ nhớ tránh spam driver khi nhiều module cùng đọc
_CACHE_TTL_SECONDS = 0.5
_last_query_time: float = 0.0
_cached_stats: Optional[Dict[str, Any]] = None

_nvml_handle = None
_nvml_initialized = False
_nvml_device = None
_device_name: str = "NVIDIA GPU"


def _init_nvml() -> bool:
    global _nvml_handle, _nvml_initialized, _nvml_device, _device_name
    if _nvml_initialized:
        return True

    try:
        # Trên Windows, nvml.dll nằm sẵn trong System32 khi cài NVIDIA Driver
        nvml = ctypes.CDLL("nvml.dll")
        res = nvml.nvmlInit_v2()
        if res != 0:
            logger.warning("nvmlInit_v2 trả về mã lỗi: %s", res)
            return False

        device = ctypes.c_void_p()
        res_dev = nvml.nvmlDeviceGetHandleByIndex_v2(0, ctypes.byref(device))
        if res_dev != 0:
            logger.warning("nvmlDeviceGetHandleByIndex_v2 lỗi: %s", res_dev)
            return False

        name_buf = ctypes.create_string_buffer(64)
        if nvml.nvmlDeviceGetName(device, name_buf, 64) == 0:
            _device_name = name_buf.value.decode("utf-8", errors="ignore")

        _nvml_handle = nvml
        _nvml_device = device
        _nvml_initialized = True
        return True
    except Exception as e:
        logger.debug("Không thể khởi tạo NVML Ctypes: %s", e)
        return False


def get_vram_stats(force_refresh: bool = False) -> Dict[str, Any]:
    """
    Lấy thông tin VRAM và trạng thái GPU hiện tại.
    Trả về dict chuẩn chứa:
    - total_mb, used_mb, free_mb, percent
    - total_gb, used_gb, free_gb
    - temperature_c, gpu_util_percent
    - gpu_name
    - status_level: 'safe' (<65%) | 'warning' (65-85%) | 'critical' (>85%)
    - can_spawn_second_worker: bool
    """
    global _last_query_time, _cached_stats

    now = time.monotonic()
    if not force_refresh and _cached_stats is not None and (now - _last_query_time) < _CACHE_TTL_SECONDS:
        return dict(_cached_stats)

    # Thử đọc qua NVML
    if _init_nvml() and _nvml_handle and _nvml_device:
        try:
            mem = _NvmlMemory()
            _nvml_handle.nvmlDeviceGetMemoryInfo(_nvml_device, ctypes.byref(mem))

            temp = ctypes.c_uint(0)
            _nvml_handle.nvmlDeviceGetTemperature(_nvml_device, 0, ctypes.byref(temp))

            util = _NvmlUtilization()
            _nvml_handle.nvmlDeviceGetUtilizationRates(_nvml_device, ctypes.byref(util))

            total_mb = round(mem.total / (1024 * 1024), 1)
            used_mb = round(mem.used / (1024 * 1024), 1)
            free_mb = round(mem.free / (1024 * 1024), 1)
            pct = round((used_mb / total_mb * 100), 1) if total_mb > 0 else 0.0

            # Phân loại mức độ an toàn
            if pct < 65.0:
                level = "safe"
                color = "#10b981"  # Emerald green
                level_desc = "🟢 An toàn"
            elif pct < 85.0:
                level = "warning"
                color = "#f59e0b"  # Amber
                level_desc = "🟡 Đang tải nặng"
            else:
                level = "critical"
                color = "#ef4444"  # Red
                level_desc = "🔴 Nguy cơ tràn VRAM"

            # Đánh giá khả thi cho 2 luồng
            # 2 luồng cần ít nhất 2000 MB VRAM trống
            can_2_workers = free_mb >= 2000.0 and pct < 85.0

            # Kiểm tra cơ chế tự động hạ luồng chống tràn VRAM
            auto_throttled = False
            throttle_reason = ""
            effective_conc = 1
            try:
                from worker_settings import get_worker_settings, trigger_vram_emergency_downgrade
                w_cfg = get_worker_settings()
                max_thresh = float(w_cfg.get("max_vram_percent_threshold", 85.0))
                min_free = float(w_cfg.get("min_free_vram_mb", 1500.0))
                if pct >= max_thresh or free_mb < min_free:
                    reason = f"VRAM chạm ngưỡng nguy hiểm: {used_mb:.0f}MB/{total_mb:.0f}MB ({pct}%), chỉ còn {free_mb:.0f}MB trống."
                    trigger_vram_emergency_downgrade(reason)
                w_cfg = get_worker_settings()
                auto_throttled = w_cfg.get("auto_throttled", False)
                throttle_reason = w_cfg.get("throttle_reason", "")
                effective_conc = w_cfg.get("effective_concurrency", 1)
            except Exception as w_err:
                logger.debug("Lỗi kiểm tra worker_settings trong vram_monitor: %s", w_err)

            stats = {
                "available": True,
                "gpu_name": _device_name,
                "total_mb": total_mb,
                "used_mb": used_mb,
                "free_mb": free_mb,
                "total_gb": round(total_mb / 1024, 2),
                "used_gb": round(used_mb / 1024, 2),
                "free_gb": round(free_mb / 1024, 2),
                "percent": pct,
                "temperature_c": int(temp.value),
                "gpu_util_percent": int(util.gpu),
                "status_level": level,
                "status_color": color,
                "status_desc": level_desc,
                "can_spawn_second_worker": can_2_workers and not auto_throttled,
                "auto_throttled": auto_throttled,
                "throttle_reason": throttle_reason,
                "effective_concurrency": effective_conc,
                "recommendation": (
                    "Đã tự động hạ về 1 luồng (Bảo vệ VRAM)" if auto_throttled else (
                        "Khả thi chạy 2 luồng so le" if can_2_workers else "Nên duy trì 1 luồng"
                    )
                ),
                "timestamp": time.time(),
            }

            _cached_stats = stats
            _last_query_time = now
            return dict(stats)

        except Exception as e:
            logger.warning("Lỗi đọc dữ liệu NVML: %s", e)

    # Fallback dự phòng nếu máy không có card NVIDIA hoặc lỗi driver
    fallback = {
        "available": False,
        "gpu_name": "Không nhận diện GPU NVIDIA",
        "total_mb": 6144.0,
        "used_mb": 0.0,
        "free_mb": 6144.0,
        "total_gb": 6.0,
        "used_gb": 0.0,
        "free_gb": 6.0,
        "percent": 0.0,
        "temperature_c": 0,
        "gpu_util_percent": 0,
        "status_level": "safe",
        "status_color": "#10b981",
        "status_desc": "🟢 Mặc định",
        "can_spawn_second_worker": True,
        "recommendation": "1 luồng khuyên dùng",
        "timestamp": time.time(),
    }
    _cached_stats = fallback
    _last_query_time = now
    return dict(fallback)


def is_vram_safe_for_gpu_step(min_free_mb: float = 1800.0) -> Tuple[bool, str]:
    """
    Kiểm tra xem VRAM hiện tại có đủ an toàn để nạp một model AI nặng (RoFormer/Whisper) hay không.
    Trả về (True, "") nếu an toàn, hoặc (False, lý do) nếu VRAM đang nguy cấp.
    """
    stats = get_vram_stats(force_refresh=True)
    if not stats.get("available"):
        return True, "Không có thông tin GPU (bỏ qua kiểm tra)"

    free_mb = stats.get("free_mb", 6000.0)
    pct = stats.get("percent", 0.0)

    if pct >= 85.0 or free_mb < min_free_mb:
        reason = (
            f"VRAM đang đầy ({stats['used_gb']}GB/{stats['total_gb']}GB - {pct}%), "
            f"chỉ còn {free_mb:.0f}MB trống (cần ít nhất {min_free_mb:.0f}MB)."
        )
        try:
            from worker_settings import trigger_vram_emergency_downgrade
            trigger_vram_emergency_downgrade(reason)
        except Exception:
            pass
        return False, reason

    return True, f"VRAM an toàn ({free_mb:.0f}MB trống)"

