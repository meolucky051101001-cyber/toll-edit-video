# -*- coding: utf-8 -*-
"""
Meta Muse Spark AI Service
Kết nối trực tiếp tới mô hình Muse Spark (muse-spark-1.3-contributor) thông qua tài khoản Meta
được lưu cấu hình tại ~/.config/muse/auth.json (hoặc biến môi trường META_API_KEY).
Hỗ trợ tạo kịch bản, ý tưởng, phân cảnh hoạt họa siêu tốc không phụ thuộc Google Gemini.
"""

from __future__ import annotations

import json
import logging
import os
import urllib.request
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

logger = logging.getLogger(__name__)

MUSE_DEFAULT_MODEL = "muse-spark-1.3-contributor"
MUSE_DEFAULT_BASE_URL = "https://api.meta.ai/v1"


def get_muse_credentials() -> Tuple[Optional[str], str]:
    """
    Tìm nạp thông tin xác thực Meta Muse Spark:
    1. Ưu tiên META_API_KEY từ biến môi trường.
    2. Nạp từ C:\\Users\\admin\\.config\\muse\\auth.json.
    """
    env_key = os.getenv("META_API_KEY")
    if env_key and env_key.strip():
        base_url = os.getenv("META_API_BASE_URL", MUSE_DEFAULT_BASE_URL)
        return env_key.strip(), base_url

    # Kiểm tra file auth.json trong thư mục người dùng
    user_home = Path.home()
    auth_candidates = [
        user_home / ".config" / "muse" / "auth.json",
        Path(r"C:\Users\admin\.config\muse\auth.json"),
    ]

    for auth_path in auth_candidates:
        if auth_path.exists():
            try:
                with open(auth_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                meta_prov = data.get("providers", {}).get("meta", {})
                api_key = meta_prov.get("api_key")
                base_url = meta_prov.get("api_base_url") or MUSE_DEFAULT_BASE_URL
                if api_key:
                    return api_key.strip(), base_url.strip()
            except Exception as e:
                logger.warning(f"Không thể đọc file {auth_path}: {e}")

    return None, MUSE_DEFAULT_BASE_URL


def is_muse_available() -> bool:
    """Kiểm tra xem hệ thống đã đăng nhập Meta Muse Spark hay chưa."""
    api_key, _ = get_muse_credentials()
    return bool(api_key)


def call_muse_spark(
    prompt: str,
    system_instruction: str = "",
    model: str = MUSE_DEFAULT_MODEL,
    temperature: float = 0.7,
    timeout: int = 75
) -> str:
    """
    Gửi prompt đến Meta Muse Spark API và nhận phản hồi văn bản.
    """
    api_key, base_url = get_muse_credentials()
    if not api_key:
        raise RuntimeError(
            "Chưa tìm thấy thông tin đăng nhập Meta Muse! Vui lòng chạy lệnh 'muse login' trong terminal để liên kết tài khoản."
        )

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "User-Agent": "Muse-Studio/1.0"
    }

    messages = []
    if system_instruction and system_instruction.strip():
        messages.append({"role": "system", "content": system_instruction.strip()})
    messages.append({"role": "user", "content": prompt.strip()})

    payload = {
        "model": model,
        "messages": messages,
        "temperature": temperature
    }

    endpoint = f"{base_url.rstrip('/')}/chat/completions"
    logger.info(f"Đang gửi yêu cầu tới Meta Muse Spark ({model})...")

    req = urllib.request.Request(
        endpoint,
        data=json.dumps(payload).encode("utf-8"),
        headers=headers
    )

    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            resp_bytes = resp.read()
            resp_data = json.loads(resp_bytes.decode("utf-8"))
            choices = resp_data.get("choices", [])
            if not choices:
                raise RuntimeError("Muse Spark trả về phản hồi rỗng.")
            content = choices[0].get("message", {}).get("content", "")
            return content.strip()
    except Exception as e:
        logger.error(f"Lỗi khi gọi Meta Muse Spark API: {e}")
        raise RuntimeError(f"Lỗi kết nối Meta Muse Spark: {e}")


def call_muse_spark_chat(
    messages: list[dict[str, str]],
    model: str = MUSE_DEFAULT_MODEL,
    temperature: float = 0.7,
    timeout: int = 75
) -> str:
    """
    Gửi danh sách hội thoại nhiều lượt (multi-turn conversation) đến Meta Muse Spark API.
    """
    api_key, base_url = get_muse_credentials()
    if not api_key:
        raise RuntimeError(
            "Chưa tìm thấy thông tin đăng nhập Meta Muse! Vui lòng liên kết tài khoản."
        )

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "User-Agent": "Muse-Studio/1.0"
    }

    formatted_messages = []
    for m in messages:
        role = m.get("role", "user")
        content = m.get("content", "")
        if role in ("user", "assistant", "system") and content:
            formatted_messages.append({"role": role, "content": str(content).strip()})

    if not formatted_messages:
        raise ValueError("Danh sách tin nhắn trống.")

    payload = {
        "model": model,
        "messages": formatted_messages,
        "temperature": temperature
    }

    endpoint = f"{base_url.rstrip('/')}/chat/completions"
    logger.info(f"Đang gửi hội thoại tới Meta Muse Spark ({model}, {len(formatted_messages)} tin nhắn)...")

    req = urllib.request.Request(
        endpoint,
        data=json.dumps(payload).encode("utf-8"),
        headers=headers
    )

    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            resp_bytes = resp.read()
            resp_data = json.loads(resp_bytes.decode("utf-8"))
            choices = resp_data.get("choices", [])
            if not choices:
                raise RuntimeError("Muse Spark trả về phản hồi rỗng.")
            content = choices[0].get("message", {}).get("content", "")
            return content.strip()
    except Exception as e:
        logger.error(f"Lỗi khi gọi Meta Muse Spark Chat API: {e}")
        raise RuntimeError(f"Lỗi kết nối Meta Muse Spark: {e}")

