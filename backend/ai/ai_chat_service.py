# -*- coding: utf-8 -*-
"""
AI Chat Co-pilot Service cho Studio Kịch Bản.
Hỗ trợ trò chuyện tương tác 2 chiều với Meta Muse Spark hoặc Google Gemini.
Cung cấp giải pháp lên ý tưởng kịch bản, gợi ý hook, tối ưu lời thoại và giải đáp thắc mắc trực tiếp trên Dashboard.
"""

from __future__ import annotations

import logging
import os
from typing import Any, Dict, List, Optional
import requests

from ai.muse_spark_service import is_muse_available, call_muse_spark_chat
from ai.scriptwriting import get_gemini_api_key, GEMINI_FALLBACK_MODELS

logger = logging.getLogger(__name__)

SYSTEM_PROMPT_CHAT = """Bạn là Trợ Lý Biên Kịch Video AI Cao Cấp (AI Video Co-pilot) trên Dashboard Studio Kịch Bản.
Nhiệm vụ của bạn:
1. Trò chuyện tự nhiên, tư vấn ý tưởng video ngắn (TikTok, YouTube Shorts, Reels), video Whiteboard/Doodle explainer, video tin tức hoặc review.
2. Giúp người dùng lên kịch bản, sáng tạo cốt truyện lôi cuốn, tinh chỉnh lời thoại, gợi ý các câu Hook mở đầu giật gân, phân tích tâm lý người xem.
3. Khi đề xuất kịch bản hoặc phân cảnh cụ thể, hãy viết rõ ràng, súc tích (12-18 từ mỗi phân cảnh).
4. Luôn trả lời bằng tiếng Việt thân thiện, thông minh, chuyên nghiệp và truyền cảm hứng.
5. Nếu người dùng hỏi về tiến trình làm video, hãy giải thích quy trình: Nhập ý tưởng/Prompt ➔ Chọn mô hình AI ➔ Chọn giọng đọc TTS ➔ AI tự sinh phân cảnh & render video 9:16."""


def chat_with_gemini(
    messages: List[Dict[str, str]],
    system_instruction: str = SYSTEM_PROMPT_CHAT,
    model: Optional[str] = None
) -> str:
    """Gọi Gemini API cho cuộc hội thoại nhiều lượt."""
    key = get_gemini_api_key()
    if not key:
        raise RuntimeError("Chưa cấu hình GEMINI_API_KEY trong hệ thống!")

    models_to_try = [model] if model else []
    for m in GEMINI_FALLBACK_MODELS:
        if m and m not in models_to_try:
            models_to_try.append(m)

    contents = []
    for m in messages:
        role = "user" if m.get("role") in ("user", "human") else "model"
        text = m.get("content", "").strip()
        if text:
            contents.append({
                "role": role,
                "parts": [{"text": text}]
            })

    if not contents:
        raise ValueError("Danh sách tin nhắn trống.")

    payload: Dict[str, Any] = {
        "contents": contents,
        "generationConfig": {
            "temperature": 0.7,
            "topP": 0.95
        }
    }
    if system_instruction:
        payload["systemInstruction"] = {"parts": [{"text": system_instruction}]}

    headers = {
        "Content-Type": "application/json",
        "x-goog-api-key": key,
    }

    last_err = None
    for m_name in models_to_try:
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{m_name}:generateContent"
        try:
            resp = requests.post(url, json=payload, headers=headers, timeout=60)
            if resp.status_code == 200:
                data = resp.json()
                text = data["candidates"][0]["content"]["parts"][0]["text"].strip()
                return text
            elif resp.status_code in (400, 401, 403):
                last_err = f"HTTP {resp.status_code}: {resp.text}"
                break
            else:
                last_err = f"HTTP {resp.status_code}: {resp.text}"
        except Exception as e:
            last_err = str(e)
            continue

    raise RuntimeError(f"Lỗi kết nối Gemini: {last_err or 'Không nhận được phản hồi'}")


def chat_with_ai(
    messages: List[Dict[str, str]],
    ai_engine: str = "muse",
    context_script: Optional[str] = None
) -> Dict[str, Any]:
    """
    Điều phối tin nhắn chat tới AI phù hợp (Meta Muse Spark hoặc Google Gemini).
    """
    sys_instruction = SYSTEM_PROMPT_CHAT
    if context_script and context_script.strip():
        sys_instruction += f"\n\n[BỐI CẢNH DỰ ÁN HIỆN TẠI CỦA NGƯỜI DÙNG]:\n{context_script.strip()}"

    selected_engine = ai_engine.lower().strip() if ai_engine else "gemini"
    has_muse = is_muse_available()
    has_gemini = bool(get_gemini_api_key())

    # Fallback thông minh nếu engine được chọn chưa sẵn sàng
    if selected_engine == "muse" and not has_muse:
        if has_gemini:
            selected_engine = "gemini"
        else:
            raise RuntimeError("Chưa đăng nhập Meta Muse Spark và chưa cấu hình GEMINI_API_KEY!")
    elif selected_engine == "gemini" and not has_gemini:
        if has_muse:
            selected_engine = "muse"
        else:
            raise RuntimeError("Chưa cấu hình GEMINI_API_KEY trong hệ thống!")

    if selected_engine == "muse":
        # Chuẩn bị system message đầu tiên cho Muse
        chat_msgs = [{"role": "system", "content": sys_instruction}]
        for m in messages:
            role = m.get("role", "user")
            content = m.get("content", "")
            if role in ("user", "assistant") and content:
                chat_msgs.append({"role": role, "content": content})
        
        reply = call_muse_spark_chat(chat_msgs)
        return {
            "reply": reply,
            "engine_used": "muse",
            "model_name": "Meta Muse Spark 1.3"
        }
    else:
        reply = chat_with_gemini(messages, system_instruction=sys_instruction)
        return {
            "reply": reply,
            "engine_used": "gemini",
            "model_name": "Google Gemini 2.5 Flash"
        }
