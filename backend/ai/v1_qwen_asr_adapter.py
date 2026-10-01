"""
v1_qwen_asr_adapter.py - Adapter Sandbox & Production Execution cho Qwen3-ASR (Giai đoạn 2).
Đặt sau Feature Flag ENABLE_QWEN_ASR hoặc lựa chọn mô hình ASR trên Dashboard.
Mặc định ưu tiên Faster-Whisper Large-v3 Turbo (siêu nhanh, tiết kiệm VRAM).
Khi người dùng chọn Qwen3-ASR, tự động kích hoạt suy luận GPU CUDA với Forced Aligner.
Tuyệt đối không chạy CPU (Fail-Closed).
"""

from __future__ import annotations

import os
import sys
import gc
import time
import math
import logging
from datetime import timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

DEFAULT_QWEN_ASR_DIR = Path(r"C:\tool v1\models\v1\qwen3_asr")
DEFAULT_QWEN_ALIGNER_DIR = Path(r"C:\tool v1\models\v1\qwen3_forced_aligner")


def is_qwen_asr_enabled() -> bool:
    """Kiểm tra feature flag ENABLE_QWEN_ASR hoặc lựa chọn mô hình V1_ASR_MODEL."""
    try:
        try:
            from ..v1_feature_flags import get_feature_flags
        except ImportError:
            from v1_feature_flags import get_feature_flags
        flags = get_feature_flags()
        if flags.get("ENABLE_QWEN_ASR") is True or flags.get("V1_ASR_MODEL") == "qwen3_asr":
            return True
    except Exception:
        pass
    val = os.getenv("ENABLE_QWEN_ASR", "false").strip().lower()
    if val in ("1", "true", "yes", "on"):
        return True
    return os.getenv("V1_ASR_MODEL", "").strip().lower() == "qwen3_asr"


def check_qwen_readiness(ignore_flag: bool = False) -> Tuple[bool, str]:
    """Kiểm tra môi trường runtime, GPU, weights và packages cho Qwen3-ASR."""
    if not ignore_flag and not is_qwen_asr_enabled():
        return False, "Feature flag ENABLE_QWEN_ASR đang tắt (Mặc định đang dùng Faster-Whisper Turbo)"

    try:
        import torch
        if not torch.cuda.is_available():
            return False, "GPU CUDA không khả dụng (Qwen3-ASR yêu cầu GPU, không chạy trên CPU)"
    except ImportError:
        return False, "Thiếu thư viện PyTorch hoặc CUDA"

    try:
        import transformers  # noqa: F401
        import qwen_asr  # noqa: F401
    except ImportError as err:
        return False, f"Thiếu package runtime: {err}"

    if not DEFAULT_QWEN_ASR_DIR.is_dir() or not (DEFAULT_QWEN_ASR_DIR / "model.safetensors").is_file():
        return False, f"Chưa tìm thấy trọng số Qwen3-ASR tại {DEFAULT_QWEN_ASR_DIR}"

    if not (DEFAULT_QWEN_ALIGNER_DIR / "config.json").is_file() or not any(DEFAULT_QWEN_ALIGNER_DIR.glob("*.safetensors")):
        return False, "Thiếu Forced Aligner: không thể tạo timeline Qwen an toàn"
    return True, "Sẵn sàng chạy Qwen3-ASR trên GPU CUDA"


def _extract_word_items(time_stamps: Any) -> List[Any]:
    """Trích xuất danh sách word items từ ForcedAlignResult hoặc list."""
    if time_stamps is None:
        return []
    if hasattr(time_stamps, "items"):
        return list(time_stamps.items)
    if isinstance(time_stamps, (list, tuple)):
        return list(time_stamps)
    return []


def group_aligned_words(word_items: List[Any], max_chars: int = 40, max_gap: float = 0.8) -> List[Dict[str, Any]]:
    """Gom nhóm các từ có mốc thời gian thành từng câu/đoạn phụ đề chuẩn SRT."""
    lines: List[Dict[str, Any]] = []
    cur_words: List[str] = []
    cur_start: Optional[float] = None
    cur_end: Optional[float] = None

    for w in word_items:
        txt = str(getattr(w, "text", "")).strip()
        if not txt:
            continue
        try:
            st = float(w.start_time)
            et = float(w.end_time)
        except (AttributeError, TypeError, ValueError) as exc:
            raise ValueError("Qwen word is missing aligned timestamps") from exc
        if not math.isfinite(st) or not math.isfinite(et) or st < 0 or et <= st:
            raise ValueError("Qwen word has invalid aligned timestamps")

        if not cur_words:
            cur_start = st
            cur_end = et
            cur_words.append(txt)
        else:
            prev_et = cur_end if cur_end is not None else st
            gap = st - prev_et
            line_txt = " ".join(cur_words)
            is_punct = cur_words[-1].endswith((".", "?", "!", ",", ":", ";", "。", "？", "！", "，", "：", "；"))
            if gap > max_gap or len(line_txt) + len(txt) + 1 > max_chars or (is_punct and len(line_txt) > 18):
                lines.append({
                    "start": round(cur_start, 3),
                    "end": round(cur_end, 3),
                    "text": line_txt,
                })
                cur_words = [txt]
                cur_start = st
                cur_end = et
            else:
                cur_words.append(txt)
                cur_end = max(cur_end if cur_end is not None else et, et)

    if cur_words:
        lines.append({
            "start": round(cur_start, 3),
            "end": round(cur_end, 3),
            "text": " ".join(cur_words),
        })
    return lines


def transcribe_audio_qwen(audio_path: str | Path, output_srt_path: Optional[str | Path] = None) -> List[Any]:
    """
    Thực thi nhận dạng âm thanh hoàn chỉnh bằng Qwen3-ASR 0.6B + Forced Aligner trên GPU CUDA.
    Trả về danh sách srt.Subtitle và tự động ghi ra file output_srt_path nếu được truyền vào.
    """
    import srt
    import torch

    path = Path(audio_path)
    if not path.is_file():
        raise FileNotFoundError(f"Không tìm thấy file audio: {path}")

    if not torch.cuda.is_available():
        raise RuntimeError("Qwen3-ASR bắt buộc chạy trên GPU CUDA theo chính sách an toàn V1 (Fail-Closed).")

    ready, reason = check_qwen_readiness(ignore_flag=True)
    if not ready:
        raise RuntimeError(f"Qwen3-ASR chưa sẵn sàng: {reason}")

    from qwen_asr import Qwen3ASRModel

    aligner_path = str(DEFAULT_QWEN_ALIGNER_DIR) if DEFAULT_QWEN_ALIGNER_DIR.is_dir() else None
    aligner_kwargs = {"dtype": torch.float16, "device_map": "cuda:0"} if aligner_path else None

    start_t = time.monotonic()
    logger.info("[QWEN_ASR] Đang nạp mô hình Qwen3-ASR 0.6B trên CUDA:0...")
    model = Qwen3ASRModel.from_pretrained(
        str(DEFAULT_QWEN_ASR_DIR),
        dtype=torch.float16,
        device_map="cuda:0",
        forced_aligner=aligner_path,
        forced_aligner_kwargs=aligner_kwargs,
    )

    try:
        logger.info("[QWEN_ASR] Bắt đầu suy luận nhận dạng âm thanh %s...", path.name)
        results = model.transcribe(
            audio=str(path),
            language=None,
            return_time_stamps=bool(aligner_path),
        )
        elapsed = time.monotonic() - start_t
        logger.info("[QWEN_ASR] Nhận dạng hoàn tất trong %.2fs", elapsed)

        if not results:
            raise RuntimeError("Qwen3-ASR không trả về kết quả nhận dạng nào.")

        res0 = results[0]
        transcript_text = getattr(res0, "text", str(res0)).strip()
        word_items = _extract_word_items(getattr(res0, "time_stamps", None))

        if word_items:
            segment_dicts = group_aligned_words(word_items)
        elif transcript_text:
            raise RuntimeError("Qwen returned text without aligned timestamps; refuse fabricated timeline")
        else:
            segment_dicts = []

        subtitles = []
        for idx, seg in enumerate(segment_dicts, start=1):
            sub = srt.Subtitle(
                index=idx,
                start=timedelta(seconds=seg["start"]),
                end=timedelta(seconds=seg["end"]),
                content=seg["text"].strip(),
            )
            subtitles.append(sub)

        if output_srt_path:
            out_p = Path(output_srt_path)
            out_p.parent.mkdir(parents=True, exist_ok=True)
            out_p.write_text(srt.compose(subtitles, reindex=False), encoding="utf-8")
            logger.info("[QWEN_ASR] Đã xuất file phụ đề SRT thành công: %s (%d câu)", out_p, len(subtitles))

        return subtitles

    finally:
        del model
        gc.collect()
        torch.cuda.empty_cache()


def run_qwen_asr_benchmark(audio_path: str | Path, use_aligner: bool = False) -> Optional[List[Dict[str, Any]]]:
    """Thực thi Qwen3-ASR ở chế độ benchmark thử nghiệm trên GPU CUDA (tương thích backward)."""
    ready, reason = check_qwen_readiness(ignore_flag=False)
    if not ready:
        logger.info("[QWEN_ASR] Bỏ qua Qwen3-ASR vì: %s. Giữ mặc định Faster-Whisper.", reason)
        return None

    path = Path(audio_path)
    if not path.is_file():
        raise FileNotFoundError(f"Không tìm thấy file audio: {path}")

    try:
        subs = transcribe_audio_qwen(audio_path)
        return [
            {
                "start": round(s.start.total_seconds(), 2),
                "end": round(s.end.total_seconds(), 2),
                "text": s.content,
                "confidence": 1.0,
            }
            for s in subs
        ]
    except Exception as exc:
        logger.warning("[QWEN_ASR] Lỗi suy luận benchmark: %s", exc)
        return None
