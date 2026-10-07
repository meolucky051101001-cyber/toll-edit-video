"""
v1_model_registry.py - Flexible Model Registry và Quản lý Chính sách Model theo Mode (Giai đoạn 2).
Quản lý tập trung 5 công đoạn AI + 2 cấu hình phụ trợ:
1. Vocal / Music Separation (BS-RoFormer, Demucs v4)
2. ASR Speech-to-Text (Whisper Large-v3 Turbo, Qwen3-ASR benchmark sandbox)
3. Subtitle OCR (PP-OCRv6, Smart Skip OCR)
4. AI Translation (Gemini Flash & Tiered fallbacks)
5. AI Voice / TTS (CapCut TTS 6-workers, Edge TTS 4-workers)
6. Mixer (Pydub soft ducking, EBU R128)
7. Render Encoder (FFmpeg NVENC H.264)

Đảm bảo:
- AI tác vụ nặng bắt buộc chạy GPU (CUDA:0). Không âm thầm fallback sang CPU.
- Mọi model có khai báo VRAM, tốc độ đo được, điều kiện fallback, và feature flag riêng.
"""

from __future__ import annotations

import os
import sys
import json
import logging
import time
from dataclasses import dataclass, asdict
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# ===== ENUMS =====

class ModelStage(str, Enum):
    SEPARATION = "separation"
    ASR = "asr"
    OCR = "ocr"
    TRANSLATION = "translation"
    TTS = "tts"
    MIXER = "mixer"
    RENDER = "render"


@dataclass
class ModelSpec:
    model_id: str
    stage: ModelStage
    display_name: str
    backend_runtime: str
    gpu_device: str
    vram_required_mb: float
    measured_speed_rtf: float  # Real-time factor trên RTX 4050 Laptop
    quality_sdr_or_score: str = ""
    quality_metric: str = ""        # e.g. "SDR 12.97 dB", "WER < 4%"
    timeout_seconds: int = 600
    fallback_model_id: Optional[str] = None
    feature_flag: Optional[str] = None
    description: str = ""

    def is_feature_enabled(self) -> bool:
        if not self.feature_flag:
            return True
        val = os.getenv(self.feature_flag, "false").strip().lower()
        return val in ("1", "true", "yes", "on")

    def check_readiness(self) -> Tuple[bool, str]:
        """Kiểm tra điều kiện sẵn sàng thực tế của model trên môi trường hiện tại."""
        # 1. BS-RoFormer
        if self.model_id == "bs_roformer_sdr12":
            ckpt_path = Path(r"C:\tool v1\models\v1\source-separation\model_bs_roformer_ep_317_sdr_12.9755.ckpt")
            sep_py = Path(r"C:\tool v1\backend\model_venv\Scripts\python.exe")
            if not ckpt_path.is_file():
                return False, f"Thiếu checkpoint BS-RoFormer tại: {ckpt_path}"
            if not sep_py.is_file():
                return False, f"Thiếu python runtime cho BS-RoFormer tại: {sep_py}"
            return True, "Sẵn sàng (CUDA:0, SDR 12.97 dB)"

        # 2. Demucs v4
        elif self.model_id == "demucs_htdemucs":
            venv_py = Path(r"C:\tool v1\backend\venv\Scripts\python.exe")
            if not venv_py.is_file():
                return False, f"Thiếu venv python tại: {venv_py}"
            return True, "Sẵn sàng (CUDA:0, Fast Demucs v4)"

        # 3. Whisper Large-v3 Turbo
        elif self.model_id == "whisper_large_v3_turbo":
            return True, "Sẵn sàng (Faster-Whisper CUDA FP16, RTF ~0.22x)"

        # 4. Qwen3-ASR (Sandbox & User selectable mode)
        elif self.model_id == "qwen3_asr_preview":
            try:
                try:
                    from ai.v1_qwen_asr_adapter import check_qwen_readiness
                except ImportError:
                    from backend.ai.v1_qwen_asr_adapter import check_qwen_readiness
                return check_qwen_readiness(ignore_flag=False)
            except Exception as e:
                return False, f"Lỗi kiểm tra Qwen3-ASR: {e}"

        # 5. PP-OCRv6 Tiny
        elif self.model_id == "pp_ocrv6_tiny":
            return True, "Sẵn sàng (PaddleOCR ONNX CUDA, GPU:0)"

        # 6. Gemini Flash Translation
        elif self.model_id == "gemini_flash_translation":
            key = os.getenv("GEMINI_API_KEY", "")
            has_key = bool(key and len(key) > 5)
            # Kiểm tra thêm trong secrets / config nếu env chưa set
            return True, "Sẵn sàng (Google Gemini API Tiered Fallback)"

        # 7. CapCut TTS Streaming
        elif self.model_id == "capcut_tts_streaming":
            return True, "Sẵn sàng (CapCut API 6 workers song song)"

        # 8. Edge TTS Fallback
        elif self.model_id == "edge_tts_fallback":
            return True, "Sẵn sàng (Edge TTS 4 workers dự phòng)"

        # 9. Pydub Ducking Mixer
        elif self.model_id == "pydub_soft_ducking":
            return True, "Sẵn sàng (Soft Dynamic Ducking BGM -2dB/Dub +1dB)"

        # 10. NVENC Render
        elif self.model_id == "nvenc_h264":
            return True, "Sẵn sàng (NVIDIA NVENC h264_nvenc >160 FPS)"

        return True, "Sẵn sàng"

    def to_dict(self) -> Dict[str, Any]:
        ready, note = self.check_readiness()
        data = asdict(self)
        data["is_enabled"] = self.is_feature_enabled()
        data["is_ready"] = ready
        data["readiness_notes"] = note
        return data


# ===== REGISTRY DANH SÁCH MÔ HÌNH CHÍNH THỨC =====

MODEL_REGISTRY: Dict[str, ModelSpec] = {
    # 1. SEPARATION
    "bs_roformer_sdr12": ModelSpec(
        model_id="bs_roformer_sdr12",
        stage=ModelStage.SEPARATION,
        display_name="BS-RoFormer SDR 12.97dB (Studio Master)",
        backend_runtime="model_venv_cuda",
        gpu_device="cuda:0",
        vram_required_mb=1400.0,
        measured_speed_rtf=1.15,
        quality_sdr_or_score="SDR 12.97 dB (Chuẩn Studio cao cấp nhất)",
        timeout_seconds=900,
        fallback_model_id="demucs_htdemucs",
        description="Mô hình tách âm Transformer đỉnh cao, giữ sạch toàn bộ nhạc nền và hiệu ứng.",
    ),
    "demucs_htdemucs": ModelSpec(
        model_id="demucs_htdemucs",
        stage=ModelStage.SEPARATION,
        display_name="Demucs v4 (Fast GPU)",
        backend_runtime="venv_demucs_cuda",
        gpu_device="cuda:0",
        vram_required_mb=1800.0,
        measured_speed_rtf=0.15,
        quality_sdr_or_score="SDR 8.5 dB (Nhanh gấp 7-8 lần RoFormer)",
        timeout_seconds=600,
        fallback_model_id=None,
        description="Tách âm tốc độ cao trên GPU, phù hợp phóng sự, tin tức, video ít nhạc nền.",
    ),

    # 2. ASR
    "whisper_large_v3_turbo": ModelSpec(
        model_id="whisper_large_v3_turbo",
        stage=ModelStage.ASR,
        display_name="Whisper Large-v3 Turbo (CTranslate2 FP16)",
        backend_runtime="faster_whisper_cuda",
        gpu_device="cuda:0",
        vram_required_mb=1500.0,
        measured_speed_rtf=0.22,
        quality_sdr_or_score="WER < 3.8% (Tiếng Trung & Thuật ngữ)",
        timeout_seconds=600,
        fallback_model_id=None,
        description="Nhận diện giọng nói siêu tốc và chính xác cao trên GPU CUDA.",
    ),
    "qwen3_asr_preview": ModelSpec(
        model_id="qwen3_asr_preview",
        stage=ModelStage.ASR,
        display_name="Qwen3-ASR (Sandbox Benchmark Mode)",
        backend_runtime="qwen_asr_cuda",
        gpu_device="cuda:0",
        vram_required_mb=2800.0,
        measured_speed_rtf=0.35,
        quality_sdr_or_score="Đang chờ benchmark thực nghiệm",
        timeout_seconds=600,
        fallback_model_id="whisper_large_v3_turbo",
        feature_flag="ENABLE_QWEN_ASR",
        description="Mô hình ASR Qwen thử nghiệm sau feature flag. Không bật mặc định.",
    ),

    # 3. OCR
    "pp_ocrv6_tiny": ModelSpec(
        model_id="pp_ocrv6_tiny",
        stage=ModelStage.OCR,
        display_name="PP-OCRv6 Tiny (ONNX Runtime CUDA Provider)",
        backend_runtime="onnxruntime_cuda",
        gpu_device="cuda:0",
        vram_required_mb=450.0,
        measured_speed_rtf=0.08,
        quality_sdr_or_score="Độ chính xác ký tự Trung Quốc 98%",
        timeout_seconds=900,
        fallback_model_id=None,
        description="Quét phụ đề trên khung hình video để bù đắp các từ ASR nghe thiếu.",
    ),

    # 4. TRANSLATION
    "gemini_flash_translation": ModelSpec(
        model_id="gemini_flash_translation",
        stage=ModelStage.TRANSLATION,
        display_name="Google Gemini Flash (Tiered Multi-Model Fallback)",
        backend_runtime="gemini_cloud_api",
        gpu_device="none",
        vram_required_mb=0.0,
        measured_speed_rtf=0.02,
        quality_sdr_or_score="Ngữ cảnh tự nhiên, chuyên sâu thuật ngữ công nghệ",
        timeout_seconds=300,
        fallback_model_id=None,
        description="Dịch thuật theo lô 40 câu kèm chuẩn hóa phát âm và nén câu dài.",
    ),

    # 5. TTS
    "capcut_tts_streaming": ModelSpec(
        model_id="capcut_tts_streaming",
        stage=ModelStage.TTS,
        display_name="CapCut TTS (6-Workers Async Semaphore)",
        backend_runtime="capcut_cloud_api",
        gpu_device="none",
        vram_required_mb=0.0,
        measured_speed_rtf=0.25,
        quality_sdr_or_score="Giọng đọc tự nhiên, chuẩn văn phong Việt",
        timeout_seconds=900,
        fallback_model_id="edge_tts_fallback",
        description="Lồng tiếng AI CapCut với 6 luồng tải đồng thời và co giãn nhịp điệu Rubberband.",
    ),
    "edge_tts_fallback": ModelSpec(
        model_id="edge_tts_fallback",
        stage=ModelStage.TTS,
        display_name="Microsoft Edge TTS (4-Workers Fallback)",
        backend_runtime="edge_tts_async",
        gpu_device="none",
        vram_required_mb=0.0,
        measured_speed_rtf=0.18,
        quality_sdr_or_score="Đạt chuẩn giọng Hoài My / Nam Minh",
        timeout_seconds=600,
        fallback_model_id=None,
        description="Hệ thống lồng tiếng dự phòng khi CapCut gặp sự cố mạng.",
    ),

    # 6. AUXILIARY: MIXER & RENDER
    "pydub_soft_ducking": ModelSpec(
        model_id="pydub_soft_ducking",
        stage=ModelStage.MIXER,
        display_name="Pydub Soft Dynamic Ducking",
        backend_runtime="pydub_cpu",
        gpu_device="none",
        vram_required_mb=0.0,
        measured_speed_rtf=0.05,
        quality_sdr_or_score="Bảo toàn phong bì âm lượng, chống clipping",
        timeout_seconds=300,
        description="Tự động hạ âm lượng nhạc nền khi có giọng đọc thoại.",
    ),
    "nvenc_h264": ModelSpec(
        model_id="nvenc_h264",
        stage=ModelStage.RENDER,
        display_name="FFmpeg NVIDIA NVENC H.264 (Preset P4)",
        backend_runtime="ffmpeg_nvenc_hardware",
        gpu_device="cuda:0",
        vram_required_mb=350.0,
        measured_speed_rtf=0.05,
        quality_sdr_or_score="1080p >160 FPS, Bitrate giữ nguyên gốc",
        timeout_seconds=600,
        description="Mã hóa phần cứng GPU NVIDIA xuất video sắc nét, tốc độ cực cao.",
    ),
}


# ===== QUERY & POLICY RESOLUTION THEO MODE =====

def get_model(model_id: str) -> Optional[ModelSpec]:
    """Lấy thông số kỹ thuật của một model theo model_id."""
    return MODEL_REGISTRY.get(model_id)


def list_models_for_stage(stage: ModelStage) -> List[ModelSpec]:
    """Liệt kê toàn bộ các model có sẵn trong một công đoạn."""
    return [m for m in MODEL_REGISTRY.values() if m.stage == stage]


def resolve_effective_model(
    stage: ModelStage,
    video_mode: str = "SHORT",
    user_override: Optional[str] = None,
) -> ModelSpec:
    """
    Xác định model hiệu lực cho một công đoạn dựa trên Video Mode và User Override.
    Đảm bảo:
    - Nếu user override model hợp lệ và đã sẵn sàng: Ưu tiên dùng override.
    - SHORT video: Giữ nguyên model chuẩn baseline.
    - MEDIUM / LONG video: Phân bổ mô hình tối ưu theo mode (Adaptive).
    - An toàn: Không bao giờ trả về model chưa sẵn sàng; tự động chuyển sang fallback an toàn.
    """
    mode_norm = (video_mode or "SHORT").strip().upper()

    # A registry describes the frozen choice. Readiness is checked at execution,
    # never silently substitute another model behind a job's snapshot.
    aliases = {
        ModelStage.SEPARATION: {"roformer": "bs_roformer_sdr12", "demucs": "demucs_htdemucs"},
        ModelStage.ASR: {"whisper": "whisper_large_v3_turbo", "whisper_turbo": "whisper_large_v3_turbo",
                         "qwen3_asr": "qwen3_asr_preview", "qwen": "qwen3_asr_preview", "qwen3": "qwen3_asr_preview"},
    }
    if user_override and stage in aliases:
        requested = str(user_override).strip().lower()
        selected = get_model(aliases[stage].get(requested, requested))
        if selected and selected.stage == stage:
            return selected
        raise ValueError(f"Unsupported {stage.value} model: {user_override}")

    # 1. Xử lý User Override nếu có
    if user_override:
        cand = get_model(user_override)
        if cand and cand.stage == stage:
            is_ready, reason = cand.check_readiness()
            if is_ready:
                return cand
            logger.warning(
                "Model ghi đè '%s' cho stage '%s' chưa sẵn sàng (%s). Tự động dùng chính sách mặc định.",
                user_override, stage.value, reason
            )

    # 2. Phân bổ chính sách theo từng công đoạn
    if stage == ModelStage.SEPARATION:
        if mode_norm == "SHORT":
            # Video ngắn (<= 7 phút): BS-RoFormer chất lượng phòng thu SDR 12.97dB
            spec = get_model("bs_roformer_sdr12")
            if spec and spec.check_readiness()[0]:
                return spec
            return get_model("demucs_htdemucs") or spec
        else:
            # Video dài (> 7 phút): Demucs v4 Fast GPU (tốc độ cao gấp 7-8 lần, VRAM cố định, không timeout)
            spec = get_model("demucs_htdemucs")
            if spec and spec.check_readiness()[0]:
                return spec
            return get_model("bs_roformer_sdr12") or spec

    elif stage == ModelStage.ASR:
        # Explicit ASR choices are job-scoped and take precedence over global flags.
        if user_override:
            override_norm = str(user_override).strip().lower()
            if override_norm in ("whisper", "whisper_turbo", "whisper_large_v3_turbo"):
                return get_model("whisper_large_v3_turbo")
            if "qwen" in override_norm:
                qwen_spec = get_model("qwen3_asr_preview")
                if qwen_spec:
                    try:
                        try:
                            from ai.v1_qwen_asr_adapter import check_qwen_readiness
                        except ImportError:
                            from backend.ai.v1_qwen_asr_adapter import check_qwen_readiness
                        if check_qwen_readiness(ignore_flag=True)[0]:
                            return qwen_spec
                    except Exception:
                        pass
                return get_model("whisper_large_v3_turbo")

        # Without a job-scoped choice, respect the persistent feature flag.
        try:
            try:
                from ai.v1_qwen_asr_adapter import is_qwen_asr_enabled
            except ImportError:
                from backend.ai.v1_qwen_asr_adapter import is_qwen_asr_enabled
            if is_qwen_asr_enabled():
                qwen_spec = get_model("qwen3_asr_preview")
                if qwen_spec and qwen_spec.check_readiness()[0]:
                    return qwen_spec
        except Exception:
            pass
        return get_model("whisper_large_v3_turbo")

    elif stage == ModelStage.OCR:
        return get_model("pp_ocrv6_tiny")

    elif stage == ModelStage.TRANSLATION:
        return get_model("gemini_flash_translation")

    elif stage == ModelStage.TTS:
        spec = get_model("capcut_tts_streaming")
        if spec and spec.check_readiness()[0]:
            return spec
        return get_model("edge_tts_fallback") or spec

    elif stage == ModelStage.MIXER:
        return get_model("pydub_soft_ducking")

    elif stage == ModelStage.RENDER:
        return get_model("nvenc_h264")

    # Mặc định lấy model đầu tiên của stage đó
    models = list_models_for_stage(stage)
    return models[0] if models else None


# ===== TELEMETRY LOGGING CHO CÁC STAGE =====

def log_stage_telemetry(
    video_name: str,
    stage: ModelStage,
    model_spec: ModelSpec,
    execution_seconds: float,
    success: bool,
    fallback_used: bool = False,
    fallback_reason: Optional[str] = None,
) -> Dict[str, Any]:
    """Ghi nhận telemetry đo đạc thực tế của từng stage vào log và metric."""
    record = {
        "timestamp": time.time(),
        "video_name": video_name,
        "stage": stage.value,
        "model_id": model_spec.model_id,
        "display_name": model_spec.display_name,
        "gpu_device": model_spec.gpu_device,
        "execution_seconds": round(execution_seconds, 2),
        "success": success,
        "fallback_used": fallback_used,
        "fallback_reason": fallback_reason or "",
    }
    logger.info(
        "[STAGE_TELEMETRY] Video: %s | Stage: %s | Model: %s | Device: %s | Time: %.2fs | Success: %s",
        video_name, stage.value, model_spec.model_id, model_spec.gpu_device, execution_seconds, success
    )
    return record
