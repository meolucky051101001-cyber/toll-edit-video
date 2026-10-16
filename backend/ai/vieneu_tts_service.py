# -*- coding: utf-8 -*-
"""
VieNeu-TTS / Vie-TTS Service for Tool V1.
Cung cấp dịch vụ tổng hợp giọng đọc tiếng Việt chất lượng cao (VieNeu 48k & Vie-TTS catalog),
hỗ trợ thread-safe lock cho GPU inference và fallback thông minh sang CapCut TTS / Edge TTS
khi môi trường chưa cài đặt gói vieneu nội bộ.
"""

from __future__ import annotations

import asyncio
import importlib.util
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

# Curated voice catalog for Tool V1 Dashboard, Bot & Pipeline
VIENEU_CURATED_VOICES: List[Dict[str, Any]] = [
    {
        "id": "vieneu-haidang",
        "label": "VieNeu · Hải Đăng (Nam Bắc 48k)",
        "source": "vieneu",
        "param": "Hải Đăng",
        "gender": "male",
        "region": "Bắc",
        "fallback_capcut": "BV075_streaming",
        "description": "Nam · Bắc · Phong cách tự nhiên, truyền cảm",
    },
    {
        "id": "vieneu-maianh",
        "label": "VieNeu · Mai Anh (Nữ Bắc 48k)",
        "source": "vieneu",
        "param": "Mai Anh",
        "gender": "female",
        "region": "Bắc",
        "fallback_capcut": "BV562_streaming",
        "description": "Nữ · Bắc · Phong cách tin tức, sắc sảo",
    },
    {
        "id": "vieneu-trucly",
        "label": "VieNeu · Trúc Ly (Nữ Bắc 48k)",
        "source": "vieneu",
        "param": "Trúc Ly",
        "gender": "female",
        "region": "Bắc",
        "fallback_capcut": "BV421_vivn_streaming",
        "description": "Nữ · Bắc · Phong cách tự nhiên, dịu dàng",
    },
    {
        "id": "vieneu-thienminh",
        "label": "VieNeu · Thiện Minh (Nam Kể chuyện 48k)",
        "source": "vieneu",
        "param": "Thiện Minh",
        "gender": "male",
        "region": "Bắc",
        "fallback_capcut": "multi_male_felipe_uranus_bigtts",
        "description": "Nam · Bắc · Phong cách kể chuyện, kịch tính",
    },
    {
        "id": "vieneu-thuydung",
        "label": "VieNeu · Thùy Dung (Nữ Nam 48k)",
        "source": "vieneu",
        "param": "Thùy Dung",
        "gender": "female",
        "region": "Nam",
        "fallback_capcut": "BV074_streaming",
        "description": "Nữ · Nam · Phong cách tin tức, phóng sự",
    },
    {
        "id": "vieneu-adambua",
        "label": "VieNeu · Adam Bựa (Nam Hài hước 48k)",
        "source": "vieneu",
        "param": "Adam bựa",
        "gender": "male",
        "region": "Bắc",
        "fallback_capcut": "BV075_streaming_vibrato_dsp",
        "description": "Nam · Bắc · Phong cách tự nhiên, dí dỏm",
    },
    {
        "id": "vieneu-thaison",
        "label": "VieNeu · Thái Sơn (Nam Nam 48k)",
        "source": "vieneu",
        "param": "Thái Sơn",
        "gender": "male",
        "region": "Nam",
        "fallback_capcut": "BV008_streaming",
        "description": "Nam · Nam · Phong cách kể chuyện, review phim",
    },
    {
        "id": "vieneu-phamtuyen",
        "label": "VieNeu · Phạm Tuyên (Nam Trầm ấm 48k)",
        "source": "vieneu",
        "param": "Phạm Tuyên",
        "gender": "male",
        "region": "Bắc",
        "fallback_capcut": "multi_male_felipe_uranus_bigtts",
        "description": "Nam · Bắc · Phong cách tự nhiên, trầm ấm",
    },
    {
        "id": "vieneu-ngochuyen",
        "label": "VieNeu · Ngọc Huyền (Nữ Tự nhiên 48k)",
        "source": "vieneu",
        "param": "Ngọc Huyền",
        "gender": "female",
        "region": "Bắc",
        "fallback_capcut": "BV562_streaming",
        "description": "Nữ · Bắc · Giọng đọc tự nhiên, cuốn hút",
    },
    {
        "id": "vieneu-ngoctran",
        "label": "VieNeu · Ngọc Trân (Nữ Miền Trung 48k)",
        "source": "vieneu",
        "param": "Ngọc Trân",
        "gender": "female",
        "region": "Trung",
        "fallback_capcut": "vi_female_huong",
        "description": "Nữ · Trung · Phong cách tự nhiên mộc mạc",
    },
    {
        "id": "vieneu-quangson",
        "label": "VieNeu · Quang Sơn (Nam Miền Trung 48k)",
        "source": "vieneu",
        "param": "Quang Sơn",
        "gender": "male",
        "region": "Trung",
        "fallback_capcut": "BV075_streaming",
        "description": "Nam · Trung · Phong cách tự nhiên, chân chất",
    },
    {
        "id": "vieneu-myduyen",
        "label": "VieNeu · Mỹ Duyên (Nữ Nam Đọc truyện 48k)",
        "source": "vieneu",
        "param": "Mỹ Duyên",
        "gender": "female",
        "region": "Nam",
        "fallback_capcut": "multi_female_yangguangnv_uranus_bigtts",
        "description": "Nữ · Nam · Phong cách đọc truyện truyền cảm",
    },
    {
        "id": "vieneu-quynhanh",
        "label": "VieNeu · Quỳnh Anh (Nữ Bắc Đọc truyện 48k)",
        "source": "vieneu",
        "param": "Quỳnh Anh",
        "gender": "female",
        "region": "Bắc",
        "fallback_capcut": "BV562_streaming",
        "description": "Nữ · Bắc · Phong cách đọc truyện, tâm sự",
    },
    {
        "id": "vieneu-thanhbinh",
        "label": "VieNeu · Thanh Bình (Nam Kể chuyện 48k)",
        "source": "vieneu",
        "param": "Thanh Bình",
        "gender": "male",
        "region": "Bắc",
        "fallback_capcut": "BV007_streaming",
        "description": "Nam · Bắc · Phong cách kể chuyện chậm rãi",
    },
]


def is_vieneu_package_available() -> bool:
    """Kiểm tra gói vieneu có được cài đặt trong môi trường Python hay không."""
    return importlib.util.find_spec("vieneu") is not None


def get_vieneu_voice_metadata(voice_param_or_id: str) -> Optional[Dict[str, Any]]:
    """Tìm thông tin metadata giọng VieNeu theo ID hoặc param name."""
    vp_clean = (voice_param_or_id or "").strip()
    for item in VIENEU_CURATED_VOICES:
        if item["id"] == vp_clean or item["param"].lower() == vp_clean.lower():
            return item
    for item in VIENEU_CURATED_VOICES:
        if vp_clean.lower() in item["param"].lower() or item["param"].lower() in vp_clean.lower():
            return item
    return None


def get_vieneu_tts_instance() -> Any:
    """Lazy singleton loader for VieNeu-TTS v3 Turbo."""
    global _vieneu_instance
    if _vieneu_instance is None:
        with _vieneu_lock:
            if _vieneu_instance is None:
                if not is_vieneu_package_available():
                    raise RuntimeError("Gói Python vieneu chưa được cài đặt trong môi trường này")
                import vieneu
                logger.info("⏳ Đang khởi tạo VieNeu-TTS v3 Turbo engine (GPU/CUDA)...")
                _vieneu_instance = vieneu.Vieneu(mode="v3turbo")
                logger.info("✅ VieNeu-TTS v3 Turbo engine đã sẵn sàng!")
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
    """
    Tạo âm thanh TTS bằng VieNeu v3 Turbo (hoặc an toàn chuyển sang CapCut TTS
    nếu gói vieneu chưa nạp trên máy).
    """
    out_file = Path(output_path).resolve()
    out_file.parent.mkdir(parents=True, exist_ok=True)

    clean_text = (text or "").strip()
    if not clean_text:
        raise ValueError("Văn bản cần đọc không được để trống")

    # Kiểm tra metadata và ánh xạ tên giọng
    meta = get_vieneu_voice_metadata(voice)
    target_voice_param = meta["param"] if meta else (voice or "Hải Đăng")

    # Nếu có gói vieneu khả dụng -> Dùng trực tiếp engine 48k
    if is_vieneu_package_available():
        try:
            tts = get_vieneu_tts_instance()
            with _vieneu_lock:
                audio = tts.infer(
                    clean_text,
                    voice=target_voice_param,
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
                        "ffmpeg", "-y", "-v", "error", "-i", str(temp_wav),
                        "-c:a", "libmp3lame", "-b:a", "192k", out_str
                    ]
                    flags = subprocess.CREATE_NO_WINDOW if hasattr(subprocess, "CREATE_NO_WINDOW") else 0
                    subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, creationflags=flags, check=True)
                finally:
                    if temp_wav.exists():
                        try:
                            temp_wav.unlink()
                        except OSError:
                            pass
            else:
                tts.save(audio, out_str)
            return out_str
        except Exception as vieneu_err:
            logger.warning("VieNeu infer gặp lỗi (%s), chuyển sang CapCut TTS fallback: %s", voice, vieneu_err)

    # Chuyển tiếp mượt mà sang CapCut TTS tương ứng
    fallback_voice = "BV562_streaming"
    if meta and meta.get("fallback_capcut"):
        fallback_voice = meta["fallback_capcut"]
    elif meta and meta.get("gender") == "male":
        fallback_voice = "BV075_streaming"

    from .voice_cloning import _run_capcut_tts
    _run_capcut_tts(clean_text, str(out_file), voice=fallback_voice)
    return str(out_file)


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
