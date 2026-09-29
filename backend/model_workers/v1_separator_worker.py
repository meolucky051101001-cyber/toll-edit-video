"""
V1 Strong-Model Audio Separator Worker.
Runs BS-RoFormer using audio-separator in an isolated subprocess with strict GPU enforcement (Native FP16).
Meets all criteria from Section 2 & 3 of the Technical Plan:
- Pure GPU execution (aborts if CUDA is unavailable; never falls back to CPU silently)
- Strict non-confusable classification of Vocals vs Instrumental (no_vocals is never mistaken for vocals)
- Rigorous post-separation sanity checks: NaN/Inf detection, duration match, channel match, non-empty, non-silence
- Full memory cleanup (CUDA cache clearing) and detailed VRAM / latency metrics logging
"""

import os
import sys
import gc
import json
import time
import math
import logging
import argparse
from pathlib import Path
from typing import List, Dict, Any, Tuple, Optional

logging.basicConfig(level=logging.INFO, format="%(asctime)s - [%(levelname)s] %(message)s")
logger = logging.getLogger("v1_separator_worker")


def get_audio_info(audio_path: str) -> Dict[str, Any]:
    """Get sample rate, channels, frames, and duration safely using soundfile or wave."""
    try:
        import soundfile as sf
        info = sf.info(audio_path)
        return {
            "duration": float(info.duration),
            "channels": int(info.channels),
            "sample_rate": int(info.samplerate),
            "frames": int(info.frames),
        }
    except Exception:
        try:
            import wave
            with wave.open(audio_path, "rb") as wf:
                sr = wf.getframerate()
                ch = wf.getnchannels()
                frames = wf.getnframes()
                dur = frames / float(sr) if sr > 0 else 0.0
                return {"duration": dur, "channels": ch, "sample_rate": sr, "frames": frames}
        except Exception:
            return {"duration": 0.0, "channels": 2, "sample_rate": 44100, "frames": 0}


def classify_outputs(output_files: List[str], output_dir: str) -> Tuple[Path, Path]:
    """
    Distinctly map and validate vocals and instrumental stems.
    Rule: Never use 'vocal' in name alone, because 'no_vocals' contains 'vocal'.
    """
    resolved = []
    for val in output_files:
        p = Path(str(val))
        if not p.is_absolute():
            p = Path(output_dir) / p
        resolved.append(p.resolve())

    # 1. Identify Instrumental stem
    instrumental = next(
        (
            p for p in resolved
            if "(instrumental)" in p.name.lower()
            or "instrumental" in p.name.lower()
            or "no_vocals" in p.name.lower()
            or "no_vocal" in p.name.lower()
            or "accompaniment" in p.name.lower()
        ),
        None
    )

    # 2. Identify Vocals stem (strictly exclude instrumental, no_vocal, no_vocals)
    vocals = next(
        (
            p for p in resolved
            if p != instrumental
            and ("(vocals)" in p.name.lower() or "vocals" in p.name.lower() or "vocal" in p.name.lower())
            and "no_vocal" not in p.name.lower()
            and "no_vocals" not in p.name.lower()
            and "instrumental" not in p.name.lower()
        ),
        None
    )

    if instrumental is None or vocals is None:
        raise RuntimeError(
            f"Không thể phân định rõ ràng 2 stem Vocals và Instrumental từ danh sách đầu ra: {[str(p) for p in resolved]}"
        )

    if vocals.resolve() == instrumental.resolve():
        raise RuntimeError(
            f"Lỗi phân loại: Đường dẫn Vocals và Instrumental trùng nhau ({vocals.resolve()})."
        )

    return vocals, instrumental


def validate_separated_audio(
    file_path: Path,
    expected_duration: float,
    stem_name: str,
    expected_channels: int = 2,
    expected_sample_rate: int = 44100,
    allow_silence: bool = False,
) -> Dict[str, Any]:
    """Validate that separated stem exists, is non-empty, has no NaN/Inf, and matches duration and channels."""
    if not file_path.is_file():
        raise FileNotFoundError(f"File đầu ra {stem_name} không tồn tại trên đĩa: {file_path}")

    size_bytes = file_path.stat().st_size
    if size_bytes < 1024:
        raise ValueError(f"File đầu ra {stem_name} quá nhỏ ({size_bytes} bytes), có dấu hiệu bị hỏng: {file_path}")

    import soundfile as sf
    import numpy as np

    info = sf.info(str(file_path))
    actual_duration = float(info.duration)

    # 1. Kiểm tra kênh và sample rate
    if info.channels < 1:
        raise ValueError(f"File {stem_name} không có luồng kênh âm thanh (channels={info.channels}).")
    if expected_channels > 0 and info.channels < expected_channels:
        raise ValueError(f"File {stem_name} có số kênh không đạt yêu cầu (có {info.channels} kênh, yêu cầu tối thiểu {expected_channels} kênh).")
    if expected_sample_rate > 0 and info.samplerate < min(expected_sample_rate, 22050):
        raise ValueError(f"Sample rate file {stem_name} quá thấp ({info.samplerate} Hz, yêu cầu tối thiểu {min(expected_sample_rate, 22050)} Hz).")
    elif info.samplerate < 16000:
        raise ValueError(f"Sample rate file {stem_name} quá thấp ({info.samplerate} Hz).")

    # 2. Kiểm tra độ lệch thời lượng (chặt chẽ: tối đa 1.0s)
    if expected_duration > 0:
        dur_diff = abs(actual_duration - expected_duration)
        allowed_tolerance = min(1.0, max(0.35, expected_duration * 0.03)) if expected_duration > 1.0 else max(0.35, expected_duration * 0.5)
        if dur_diff > allowed_tolerance:
            raise ValueError(
                f"Độ dài file {stem_name} ({actual_duration:.2f}s) lệch quá nhiều so với nguồn "
                f"({expected_duration:.2f}s, diff={dur_diff:.2f}s > {allowed_tolerance:.2f}s)."
            )

    # 3. Quét toàn bộ file qua streaming blocks để kiểm tra NaN / Inf / Peak / Năng lượng âm học
    total_energy = 0.0
    total_samples = 0
    max_peak = 0.0
    has_nan = False
    has_inf = False

    with sf.SoundFile(str(file_path)) as sfile:
        while True:
            block = sfile.read(frames=65536, dtype="float32")
            if len(block) == 0:
                break
            if np.isnan(block).any():
                has_nan = True
                break
            if np.isinf(block).any():
                has_inf = True
                break
            block_peak = float(np.max(np.abs(block)))
            if block_peak > max_peak:
                max_peak = block_peak
            total_energy += float(np.sum(block ** 2))
            total_samples += block.size

    if has_nan:
        raise ValueError(f"File {stem_name} chứa giá trị NaN (Not a Number), mô hình bị tràn số!")
    if has_inf:
        raise ValueError(f"File {stem_name} chứa giá trị Inf (Vô cực), mô hình bị lỗi số học!")

    # RMS dBFS calculation
    if total_samples > 0 and total_energy > 0:
        rms = math.sqrt(total_energy / total_samples)
        rms_dbfs = 20.0 * math.log10(max(1e-9, rms))
    else:
        rms_dbfs = -120.0

    if not allow_silence and (max_peak == 0.0 or rms_dbfs < -70.0):
        raise ValueError(
            f"File {stem_name} là âm thanh câm / hoàn toàn im lặng (RMS: {rms_dbfs:.1f} dBFS, Peak: {max_peak:.4f}). Tách âm lỗi!"
        )

    if max_peak > 1.5:
        logger.warning(f"File {stem_name} có đỉnh âm thanh vượt quá 1.0 (peak={max_peak:.2f}), có thể bị clipping nhẹ.")

    return {
        "duration": actual_duration,
        "channels": info.channels,
        "sample_rate": info.samplerate,
        "size_bytes": size_bytes,
        "peak": max_peak,
        "rms_dbfs": round(rms_dbfs, 2),
    }


def main():
    parser = argparse.ArgumentParser(description="Tool V1 BS-RoFormer Audio Separator Worker (GPU Native FP16)")
    parser.add_argument("--input-audio", required=True, help="Đường dẫn file âm thanh đầu vào")
    parser.add_argument("--output-dir", required=True, help="Thư mục xuất file tách âm")
    parser.add_argument("--model-dir", required=True, help="Thư mục chứa file trọng số checkpoint")
    parser.add_argument("--model-name", default="model_bs_roformer_ep_317_sdr_12.9755.ckpt", help="Tên file checkpoint")
    parser.add_argument("--use-fp16", action="store_true", default=True, help="Native FP16 precision")
    parser.add_argument("--output-json", default=None, help="File ghi kết quả JSON")
    args = parser.parse_args()

    t_start = time.monotonic()
    input_audio = os.path.abspath(args.input_audio)
    output_dir = os.path.abspath(args.output_dir)
    model_dir = os.path.abspath(args.model_dir)

    if not os.path.isfile(input_audio):
        err = f"File âm thanh đầu vào không tồn tại: {input_audio}"
        logger.error(err)
        print(json.dumps({"success": False, "error": err}, ensure_ascii=False))
        sys.exit(1)

    os.makedirs(output_dir, exist_ok=True)
    os.makedirs(model_dir, exist_ok=True)

    # 1. GPU Enforcement Check (Section 2: Không tự chuyển sang CPU khi CUDA thiếu hoặc hết VRAM)
    try:
        import torch
    except ImportError:
        err = "PyTorch chưa được cài đặt trong môi trường chạy tách âm."
        logger.error(err)
        print(json.dumps({"success": False, "error": err}, ensure_ascii=False))
        sys.exit(10)

    if not torch.cuda.is_available():
        err = "GPU CUDA không khả dụng trên hệ thống. BS-RoFormer yêu cầu GPU; không chạy trên CPU."
        logger.error(err)
        print(json.dumps({"success": False, "error": err}, ensure_ascii=False))
        sys.exit(11)

    device_name = torch.cuda.get_device_name(0)
    free_vram_bytes, total_vram_bytes = torch.cuda.mem_get_info()
    free_vram_mb = free_vram_bytes / (1024 * 1024)
    total_vram_mb = total_vram_bytes / (1024 * 1024)
    logger.info(f"GPU: {device_name} (VRAM khả dụng: {free_vram_mb:.0f}MB / {total_vram_mb:.0f}MB)")

    if free_vram_mb < 600:
        logger.warning(f"VRAM khả dụng thấp ({free_vram_mb:.0f}MB). Đang thu hồi bộ nhớ GPU...")
        gc.collect()
        torch.cuda.empty_cache()

    # 2. Source Audio Inspection
    src_info = get_audio_info(input_audio)
    logger.info(f"Nguồn: {src_info['duration']:.2f}s, {src_info['channels']} kênh, {src_info['sample_rate']}Hz")

    # 3. Model & Separator Initialization
    try:
        from audio_separator.separator import Separator
    except ImportError as exc:
        err = f"Thư viện audio-separator chưa được cài đặt trong runtime: {exc}"
        logger.error(err)
        print(json.dumps({"success": False, "error": err}, ensure_ascii=False))
        sys.exit(12)

    model_ckpt_path = os.path.join(model_dir, args.model_name)
    if not os.path.isfile(model_ckpt_path):
        err = f"Checkpoint model BS-RoFormer không tồn tại tại: {model_ckpt_path}"
        logger.error(err)
        print(json.dumps({"success": False, "error": err}, ensure_ascii=False))
        sys.exit(13)

    t_load_start = time.monotonic()
    torch.cuda.reset_peak_memory_stats()

    sep = None
    try:
        sep = Separator(
            log_level=logging.WARNING,
            output_dir=output_dir,
            model_file_dir=model_dir,
            output_format="WAV",
            use_soundfile=False,  # Use PyDub/FFmpeg writer to preserve PCM WAV without subtype mismatch
            use_native_fp16=True,  # Native FP16 on RTX 4050 Tensor Cores
        )
        sep.load_model(model_filename=args.model_name)
        load_time_s = time.monotonic() - t_load_start
        logger.info(f"Đã nạp model {args.model_name} vào GPU ({load_time_s:.2f}s). Bắt đầu suy luận...")

        t_infer_start = time.monotonic()
        raw_outputs = sep.separate(input_audio)
        infer_time_s = time.monotonic() - t_infer_start
        total_time_s = time.monotonic() - t_start

        peak_vram_mb = torch.cuda.max_memory_allocated() / (1024 * 1024)
        logger.info(f"Tách âm hoàn tất ({infer_time_s:.2f}s, Peak VRAM: {peak_vram_mb:.1f}MB).")

        # 4. Classify outputs
        vocals_path, bg_path = classify_outputs(raw_outputs, output_dir)

        # 5. Sanity check outputs (Chấp nhận trường hợp nguồn thực sự không có nhạc hoặc không có giọng)
        voc_meta = validate_separated_audio(
            vocals_path, src_info["duration"], "Vocals",
            expected_channels=src_info["channels"],
            expected_sample_rate=src_info["sample_rate"],
            allow_silence=True,
        )
        bg_meta = validate_separated_audio(
            bg_path, src_info["duration"], "Instrumental",
            expected_channels=src_info["channels"],
            expected_sample_rate=src_info["sample_rate"],
            allow_silence=True,
        )

        voc_is_silent = (voc_meta["peak"] == 0.0 or voc_meta["rms_dbfs"] < -70.0)
        bg_is_silent = (bg_meta["peak"] == 0.0 or bg_meta["rms_dbfs"] < -70.0)

        if voc_is_silent and bg_is_silent:
            raise ValueError(
                f"Cả hai file Vocals và Instrumental đều câm / hoàn toàn im lặng "
                f"(Vocals RMS: {voc_meta['rms_dbfs']} dBFS, Instrumental RMS: {bg_meta['rms_dbfs']} dBFS). Tách âm thất bại!"
            )
        if bg_is_silent:
            logger.info(
                f"File Instrumental im lặng (RMS: {bg_meta['rms_dbfs']} dBFS), nhưng Vocals có năng lượng ({voc_meta['rms_dbfs']} dBFS). "
                f"Chấp nhận: Video nguồn thực sự không có nhạc nền."
            )
        elif voc_is_silent:
            logger.info(
                f"File Vocals im lặng (RMS: {voc_meta['rms_dbfs']} dBFS), nhưng Instrumental có năng lượng ({bg_meta['rms_dbfs']} dBFS). "
                f"Chấp nhận: Video nguồn thực sự không có giọng nói."
            )

        result_payload = {
            "success": True,
            "vocals_path": str(vocals_path),
            "background_path": str(bg_path),
            "model": args.model_name,
            "precision": "native_fp16",
            "device": "cuda:0",
            "device_name": device_name,
            "load_time_s": round(load_time_s, 2),
            "inference_time_s": round(infer_time_s, 2),
            "total_time_s": round(total_time_s, 2),
            "peak_vram_mb": round(peak_vram_mb, 1),
            "vocals_duration_s": round(voc_meta["duration"], 2),
            "background_duration_s": round(bg_meta["duration"], 2),
            "source_duration_s": round(src_info["duration"], 2),
            "channels": bg_meta["channels"],
            "sample_rate": bg_meta["sample_rate"],
        }

        output_str = json.dumps(result_payload, ensure_ascii=False)
        if args.output_json:
            Path(args.output_json).write_text(output_str, encoding="utf-8")
        print(output_str)
        sys.exit(0)

    except torch.cuda.OutOfMemoryError as oom_err:
        logger.error(f"Lỗi tràn bộ nhớ GPU (OOM) khi chạy BS-RoFormer: {oom_err}")
        err_payload = {
            "success": False,
            "error": "CUDA Out of Memory (OOM)",
            "retryable": True,
            "detail": str(oom_err),
        }
        output_str = json.dumps(err_payload, ensure_ascii=False)
        if args.output_json:
            Path(args.output_json).write_text(output_str, encoding="utf-8")
        print(output_str)
        sys.exit(20)

    except Exception as exc:
        logger.exception(f"Lỗi không mong muốn trong worker tách âm: {exc}")
        err_payload = {
            "success": False,
            "error": str(exc),
            "retryable": False,
        }
        output_str = json.dumps(err_payload, ensure_ascii=False)
        if args.output_json:
            Path(args.output_json).write_text(output_str, encoding="utf-8")
        print(output_str)
        sys.exit(1)

    finally:
        # 6. Ensure GPU resources and Python memory are cleanly released
        if sep is not None:
            del sep
        gc.collect()
        if torch.cuda.is_available():
            torch.cuda.empty_cache()


if __name__ == "__main__":
    main()
