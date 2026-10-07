# -*- coding: utf-8 -*-
"""VieNeu-TTS (v3 Turbo, 48kHz Vietnamese Neural TTS) Service for Tool V2.

Provides ultra-fast high-fidelity Vietnamese text-to-speech generation
with 25+ authentic regional voices across North, South, and Central Vietnam.
"""

from __future__ import annotations

import asyncio
import logging
import os
import subprocess
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

logger = logging.getLogger(__name__)

# Singleton Vieneu engine instance & thread-safe lock for GPU inference
_vieneu_instance: Optional[Any] = None
_vieneu_lock = threading.Lock()

# Curated voice catalog for Tool V2 Dashboard & Bot
VIENEU_CURATED_VOICES: List[Dict[str, Any]] = [
    {
        "id": "vieneu-haidang",
        "label": "VieNeu · Hải Đăng (Nam Bắc 48k)",
        "source": "vieneu",
        "param": "Hải Đăng",
        "gender": "male",
        "region": "Bắc",
        "description": "Nam · Bắc · Phong cách tự nhiên, truyền cảm",
    },
    {
        "id": "vieneu-maianh",
        "label": "VieNeu · Mai Anh (Nữ Bắc 48k)",
        "source": "vieneu",
        "param": "Mai Anh",
        "gender": "female",
        "region": "Bắc",
        "description": "Nữ · Bắc · Phong cách tin tức, sắc sảo",
    },
    {
        "id": "vieneu-trucly",
        "label": "VieNeu · Trúc Ly (Nữ Bắc 48k)",
        "source": "vieneu",
        "param": "Trúc Ly",
        "gender": "female",
        "region": "Bắc",
        "description": "Nữ · Bắc · Phong cách tự nhiên, dịu dàng",
    },
    {
        "id": "vieneu-thienminh",
        "label": "VieNeu · Thiện Minh (Nam Kể chuyện 48k)",
        "source": "vieneu",
        "param": "Thiện Minh",
        "gender": "male",
        "region": "Bắc",
        "description": "Nam · Bắc · Phong cách kể chuyện, kịch tính",
    },
    {
        "id": "vieneu-thuydung",
        "label": "VieNeu · Thùy Dung (Nữ Nam 48k)",
        "source": "vieneu",
        "param": "Thùy Dung",
        "gender": "female",
        "region": "Nam",
        "description": "Nữ · Nam · Phong cách tin tức, phóng sự",
    },
    {
        "id": "vieneu-adambua",
        "label": "VieNeu · Adam Bựa (Nam Hài hước 48k)",
        "source": "vieneu",
        "param": "Adam bựa",
        "gender": "male",
        "region": "Bắc",
        "description": "Nam · Bắc · Phong cách tự nhiên, dí dỏm",
    },
    {
        "id": "vieneu-thaison",
        "label": "VieNeu · Thái Sơn (Nam Nam 48k)",
        "source": "vieneu",
        "param": "Thái Sơn",
        "gender": "male",
        "region": "Nam",
        "description": "Nam · Nam · Phong cách kể chuyện, review phim",
    },
    {
        "id": "vieneu-phamtuyen",
        "label": "VieNeu · Phạm Tuyên (Nam Trầm ấm 48k)",
        "source": "vieneu",
        "param": "Phạm Tuyên",
        "gender": "male",
        "region": "Bắc",
        "description": "Nam · Bắc · Phong cách tự nhiên, trầm ấm",
    },
    {
        "id": "vieneu-ngochuyen",
        "label": "VieNeu · Ngọc Huyền (Nữ Tự nhiên 48k)",
        "source": "vieneu",
        "param": "Ngọc Huyền",
        "gender": "female",
        "region": "Bắc",
        "description": "Nữ · Bắc · Giọng đọc tự nhiên, cuốn hút",
    },
    {
        "id": "vieneu-ngoctran",
        "label": "VieNeu · Ngọc Trân (Nữ Miền Trung 48k)",
        "source": "vieneu",
        "param": "Ngọc Trân",
        "gender": "female",
        "region": "Trung",
        "description": "Nữ · Trung · Phong cách tự nhiên mộc mạc",
    },
    {
        "id": "vieneu-quangson",
        "label": "VieNeu · Quang Sơn (Nam Miền Trung 48k)",
        "source": "vieneu",
        "param": "Quang Sơn",
        "gender": "male",
        "region": "Trung",
        "description": "Nam · Trung · Phong cách tự nhiên, chân chất",
    },
    {
        "id": "vieneu-myduyen",
        "label": "VieNeu · Mỹ Duyên (Nữ Nam Đọc truyện 48k)",
        "source": "vieneu",
        "param": "Mỹ Duyên",
        "gender": "female",
        "region": "Nam",
        "description": "Nữ · Nam · Phong cách đọc truyện truyền cảm",
    },
    {
        "id": "vieneu-quynhanh",
        "label": "VieNeu · Quỳnh Anh (Nữ Bắc Đọc truyện 48k)",
        "source": "vieneu",
        "param": "Quỳnh Anh",
        "gender": "female",
        "region": "Bắc",
        "description": "Nữ · Bắc · Phong cách đọc truyện, tâm sự",
    },
    {
        "id": "vieneu-thanhbinh",
        "label": "VieNeu · Thanh Bình (Nam Kể chuyện 48k)",
        "source": "vieneu",
        "param": "Thanh Bình",
        "gender": "male",
        "region": "Bắc",
        "description": "Nam · Bắc · Phong cách kể chuyện chậm rãi",
    },
]


def get_vieneu_tts_instance() -> Any:
    """Lazy singleton loader for VieNeu-TTS v3 Turbo."""
    global _vieneu_instance
    if _vieneu_instance is None:
        with _vieneu_lock:
            if _vieneu_instance is None:
                try:
                    import vieneu
                    logger.info("⏳ Đang khởi tạo VieNeu-TTS v3 Turbo engine (GPU/CUDA)...")
                    _vieneu_instance = vieneu.Vieneu(mode="v3turbo")
                    logger.info("✅ VieNeu-TTS v3 Turbo engine đã sẵn sàng!")
                except Exception as e:
                    logger.error(f"❌ Không thể nạp VieNeu-TTS v3 Turbo: {e}", exc_info=True)
                    raise
    return _vieneu_instance


def generate_tts_vieneu_sync(
    text: str,
    output_path: Union[str, Path],
    voice: str = "Hải Đăng",
    apply_watermark: bool = False,
    denoise: bool = True,
    temperature: float = 0.8,
    repetition_penalty: float = 1.2,
    **kwargs: Any,
) -> str:
    """Synchronous VieNeu TTS generation with thread-safe lock."""
    out_file = Path(output_path).resolve()
    out_file.parent.mkdir(parents=True, exist_ok=True)

    clean_text = (text or "").strip()
    if not clean_text:
        raise ValueError("Văn bản cần đọc không được để trống")

    tts = get_vieneu_tts_instance()

    target_voice = voice or "Hải Đăng"
    # Resolve aliases if user passes raw ID like 'vieneu-maianh'
    for item in VIENEU_CURATED_VOICES:
        if item["id"] == target_voice or item["param"] == target_voice:
            target_voice = item["param"]
            break

    with _vieneu_lock:
        audio = tts.infer(
            clean_text,
            voice=target_voice,
            apply_watermark=apply_watermark,
            denoise=denoise,
            temperature=temperature,
            repetition_penalty=repetition_penalty,
            **kwargs,
        )

    out_str = str(out_file)
    if out_str.lower().endswith(".mp3"):
        temp_wav = out_file.with_name(f"{out_file.stem}_vieneu_raw.wav")
        try:
            tts.save(audio, str(temp_wav))
            cmd = [
                "ffmpeg",
                "-y",
                "-v",
                "error",
                "-i",
                str(temp_wav),
                "-c:a",
                "libmp3lame",
                "-b:a",
                "192k",
                out_str,
            ]
            flags = (
                subprocess.CREATE_NO_WINDOW
                if hasattr(subprocess, "CREATE_NO_WINDOW")
                else 0
            )
            subprocess.run(
                cmd,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                creationflags=flags,
                check=True,
            )
        finally:
            if temp_wav.exists():
                try:
                    temp_wav.unlink()
                except OSError:
                    pass
    else:
        tts.save(audio, out_str)

    return out_str


async def generate_tts_vieneu(
    text: str,
    output_path: Union[str, Path],
    voice: str = "Hải Đăng",
    apply_watermark: bool = False,
    denoise: bool = True,
    temperature: float = 0.8,
    repetition_penalty: float = 1.2,
    **kwargs: Any,
) -> str:
    """Asynchronous wrapper for VieNeu TTS."""
    return await asyncio.to_thread(
        generate_tts_vieneu_sync,
        text=text,
        output_path=output_path,
        voice=voice,
        apply_watermark=apply_watermark,
        denoise=denoise,
        temperature=temperature,
        repetition_penalty=repetition_penalty,
        **kwargs,
    )
