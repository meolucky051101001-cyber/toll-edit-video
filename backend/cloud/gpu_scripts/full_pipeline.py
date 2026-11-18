"""
full_pipeline.py - Pipeline lồng tiếng tự động 8 bước chuẩn V1 chạy TRỰC TIẾP trên Colab VM.
Tận dụng GPU T4/L4 để tăng tốc:
  1. Trích xuất âm thanh từ video.
  2. Tách vocal và nhạc nền sạch (Demucs htdemucs GPU).
  3. ASR Whisper Large-v3-turbo trên audio gốc, thử lại vocal khi cần.
  4. Dịch phụ đề tự nhiên (Gemini 2.0 Flash AI).
  5. TTS CapCut Mai (BV562_streaming), không tự fallback.
  6. Hòa âm Dynamic Ducking EBU R128 (FFmpeg sidechain + loudnorm).
  7. Render video thành phẩm (FFmpeg libx264 + aac).
  8. Xuất metadata & nghiệm thu.
"""

import argparse
import asyncio
from datetime import timedelta
from difflib import SequenceMatcher
import hashlib
import json
import math
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

# Đảm bảo import được các module đi kèm như mixer.py
_CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))
if _CURRENT_DIR not in sys.path:
    sys.path.insert(0, _CURRENT_DIR)


PIPELINE_RESULT_PREFIX = "PIPELINE_RESULT_JSON:"
PIPELINE_VERSION = "v1_cloud_phase9_full_v1_parity_v1"
WHISPER_MODEL = "large-v3-turbo"
ASR_MIN_AVG_LOGPROB = -1.0  # Whisper's default decoder failure threshold.

VOICE_FEMALE_PARAM = "BV562_streaming"
VOICE_MALE_PARAM = "BV075_streaming"
PITCH_BOUNDARY_HZ = 180.0
MIN_VOICED_FRAMES = 12
CONFIDENCE_THRESHOLD = 0.70


DEFAULT_V1_RUNTIME_PROFILE: dict = {
    "schema_version": 1,
    "canvas": {
        "aspect_ratio": "9:16",
        "auto_crop_black_bars": True,
        "sub_cover_style": "localized_blur",
        "sub_blur_radius": 16,
        "sub_blur_padding_x": 0.015,
        "sub_blur_padding_y": 0.008,
        "sub_font_name": "Arial",
        "sub_font_color": "#000000",
        "sub_font_bold": True,
        "delogo_mode": "off",
    },
    "audio": {
        "bgm_volume_db": -2.0,
        "dubbing_volume_db": 1.0,
        "separation_mode": "roformer",
        "ducking_mode": "soft",
        "script_mode": "default",
    },
    "auto_voice": {
        "enabled": True,
        "female_voice_id": "capcut-BV562_streaming",
        "male_voice_id": "capcut-BV075_streaming",
        "dual_voice": False,
    },
    "glossary": {},
}


def load_v1_runtime_profile(workdir: str | Path = ".") -> dict:
    """Load frozen Tool V1 Local profile bundled into /content/v1_runtime_profile.json."""
    profile = {
        "schema_version": 1,
        "canvas": dict(DEFAULT_V1_RUNTIME_PROFILE["canvas"]),
        "audio": dict(DEFAULT_V1_RUNTIME_PROFILE["audio"]),
        "auto_voice": dict(DEFAULT_V1_RUNTIME_PROFILE["auto_voice"]),
        "glossary": {},
    }
    profile_path = Path(workdir) / "v1_runtime_profile.json"
    if profile_path.is_file():
        try:
            raw = json.loads(profile_path.read_text(encoding="utf-8-sig"))
            if isinstance(raw, dict):
                for section in ("canvas", "audio", "auto_voice"):
                    sec_val = raw.get(section)
                    if isinstance(sec_val, dict):
                        profile[section].update(sec_val)
                gloss = raw.get("glossary")
                if isinstance(gloss, dict):
                    profile["glossary"] = {
                        str(k).strip(): str(v).strip()
                        for k, v in gloss.items()
                        if str(k).strip() and str(v).strip()
                    }
        except Exception as exc:
            print(f"  [WARN] Không đọc được v1_runtime_profile.json ({exc}); dùng cấu hình chuẩn V1.", flush=True)

    env_cover = os.getenv("V1_SUBTITLE_COVER_STYLE", "").strip().lower()
    if env_cover in {"solid_box", "adaptive_box", "frosted_blur", "localized_blur"}:
        profile["canvas"]["sub_cover_style"] = env_cover
    elif str(profile["canvas"].get("sub_cover_style") or "").strip().lower() not in {
        "solid_box",
        "adaptive_box",
        "frosted_blur",
        "localized_blur",
    }:
        profile["canvas"]["sub_cover_style"] = "localized_blur"
    return profile


def _hex_to_ass_color(color_str: str) -> str:
    raw = str(color_str or "").strip()
    if re.fullmatch(r"&H[0-9A-Fa-f]{6,8}&?", raw):
        return raw
    if re.fullmatch(r"#[0-9A-Fa-f]{6}", raw):
        return f"&H00{raw[5:7].upper()}{raw[3:5].upper()}{raw[1:3].upper()}"
    return "&H00000000"


class PipelineStageError(RuntimeError):
    """Error carrying structured CloudErrorCode across Cloud pipeline stages."""

    def __init__(self, error_code: str, message: str):
        super().__init__(message)
        self.error_code = error_code


def _file_sha256(path_obj: str | Path) -> str:
    h = hashlib.sha256()
    with open(path_obj, "rb") as f:
        while True:
            chunk = f.read(1024 * 1024)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


_SECRET_ENV_NAMES = ("GEMINI_API_KEY", "GEMINI_API_KEYS", "TELEGRAM_BOT_TOKEN", "OPENAI_API_KEY", "HF_TOKEN")
_GOOGLE_API_KEY_RE = re.compile(r"AIza[0-9A-Za-z\-_]{20,}")
_BEARER_RE = re.compile(r"(?i)(authorization[\"']?\s*[:=]\s*[\"']?bearer\s+)[^\s\"',;]+")
_KV_SECRET_RE = re.compile(
    r"(?i)\b(gemini_api_key|api_key|apikey|access_token|refresh_token|bot_token|token|secret|password|key)"
    r"(\s*[:=]\s*[\"']?)([^\s&\"',;]+)"
)


def _redact_secrets_local(text: str, extra_secrets: tuple[str, ...] = ()) -> str:
    if not text:
        return ""
    scrubbed = str(text)
    candidates: list[str] = []
    for name in _SECRET_ENV_NAMES:
        val = os.getenv(name, "").strip()
        if val and len(val) >= 6:
            candidates.append(val)
    for item in extra_secrets:
        if item and len(str(item).strip()) >= 6:
            candidates.append(str(item).strip())
    for secret_val in sorted(set(candidates), key=len, reverse=True):
        scrubbed = scrubbed.replace(secret_val, "[REDACTED]")
    scrubbed = _GOOGLE_API_KEY_RE.sub("[REDACTED_API_KEY]", scrubbed)
    scrubbed = _BEARER_RE.sub(r"\1[REDACTED]", scrubbed)
    scrubbed = _KV_SECRET_RE.sub(r"\1\2[REDACTED]", scrubbed)
    return scrubbed


_STEP_PERCENT_MAP = {
    0: 55,
    1: 56,
    2: 58,
    3: 61,
    4: 68,
    5: 71,
    6: 82,
    7: 84,
    8: 84,
}


def _write_pipeline_progress(percent: int, step_num: int, total: int, message: str) -> None:
    safe_msg = _redact_secrets_local(message)
    payload = {
        "percent": max(55, min(84, int(percent))),
        "step": int(step_num),
        "total": int(total),
        "message": f"[{step_num}/{total}] {safe_msg}" if step_num > 0 else safe_msg,
        "updated_at": round(time.time(), 3),
    }
    tmp_prog = ".pipeline_progress.json.tmp"
    try:
        with open(tmp_prog, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False)
        os.replace(tmp_prog, ".pipeline_progress.json")
    except OSError:
        pass


def log_step(step_num: int, total: int, message: str):
    safe_msg = _redact_secrets_local(message)
    print(f"\n[{step_num}/{total}] {safe_msg}", flush=True)
    _write_pipeline_progress(_STEP_PERCENT_MAP.get(int(step_num), 55), step_num, total, safe_msg)


def run_cmd(cmd: list[str], desc: str):
    print(f"  -> Chạy: {' '.join(cmd[:6])}...", flush=True)
    res = subprocess.run(cmd, capture_output=True, text=True)
    if res.returncode != 0:
        safe_stderr = _redact_secrets_local(res.stderr[-600:])
        print(f"  [ERROR] {desc} thất bại:\n{safe_stderr}", file=sys.stderr, flush=True)
        raise RuntimeError(f"{desc} thất bại (rc={res.returncode})")
    return res.stdout


def step1_extract_audio(input_video: str, output_wav: str):
    log_step(1, 8, "Trích xuất âm thanh từ video...")
    cmd = [
        "ffmpeg", "-y", "-i", input_video,
        "-vn", "-acodec", "pcm_s16le", "-ar", "44100", "-ac", "2",
        output_wav
    ]
    run_cmd(cmd, "Trích xuất âm thanh")
    print(f"  -> Đã trích xuất: {output_wav}", flush=True)


def step2_separate_vocals(audio_path: str, work_dir: str, *, separation_mode: str = "roformer") -> tuple[str, str]:
    """
    Bóc tách giọng nói gốc (Vocals) và Nhạc nền (BGM/Instrumental).
    Ưu tiên BS-RoFormer (audio-separator), fallback sang Demucs htdemucs GPU.
    Trả về (vocals_path, bgm_path).
    """
    sep_mode = str(separation_mode or "roformer").strip().lower()
    if sep_mode == "bypass":
        log_step(2, 8, "Chế độ separation=bypass: giữ nguyên audio gốc...")
        return audio_path, audio_path

    log_step(2, 8, f"Bóc tách giọng nói & nhạc nền ({sep_mode.upper()} -> Demucs GPU)...")
    import torch

    device = "cuda" if torch.cuda.is_available() else "cpu"
    sep_dir = os.path.join(work_dir, "separated")
    os.makedirs(sep_dir, exist_ok=True)

    # 1. Thử BS-RoFormer trước nếu chế độ roformer và thư viện audio-separator khả dụng
    if sep_mode == "roformer":
        try:
            from audio_separator.separator import Separator
            roformer_dir = os.path.join(sep_dir, "bs_roformer")
            model_cache = os.path.join(work_dir, "model_cache")
            os.makedirs(roformer_dir, exist_ok=True)
            os.makedirs(model_cache, exist_ok=True)
            model_name = "model_bs_roformer_ep_317_sdr_12.9755.ckpt"
            print(f"  -> Đang tách âm bằng BS-RoFormer ({model_name}, device={device})...", flush=True)
            sep = Separator(
                output_dir=roformer_dir,
                model_file_dir=model_cache,
                output_format="WAV",
                use_soundfile=False,
                use_native_fp16=torch.cuda.is_available(),
            )
            sep.load_model(model_filename=model_name)
            raw_outputs = sep.separate(audio_path)
            resolved = [
                p if os.path.isabs(str(p)) else os.path.join(roformer_dir, str(p))
                for p in (raw_outputs or [])
            ]
            bgm_wav = next(
                (p for p in resolved if "instrumental" in os.path.basename(p).lower() or "no_vocal" in os.path.basename(p).lower()),
                None,
            )
            vocals_wav = next(
                (p for p in resolved if p != bgm_wav and "vocal" in os.path.basename(p).lower() and "no_vocal" not in os.path.basename(p).lower()),
                None,
            )
            if vocals_wav and bgm_wav and os.path.isfile(vocals_wav) and os.path.isfile(bgm_wav):
                print(f"  -> BS-RoFormer tách âm thành công! Vocals: {vocals_wav} | BGM: {bgm_wav}", flush=True)
                return vocals_wav, bgm_wav
        except Exception as exc:
            print(f"  -> BS-RoFormer không khả dụng ({exc}); chuyển sang Demucs {device}...", flush=True)

    print(f"  -> Demucs chạy trên thiết bị: {device}", flush=True)
    cmd = [
        sys.executable, "-m", "demucs",
        "-n", "htdemucs",
        "--two-stems", "vocals",
        "-d", device,
        "-o", sep_dir,
        audio_path
    ]

    try:
        run_cmd(cmd, "Demucs vocal separation")
        base_name = os.path.splitext(os.path.basename(audio_path))[0]
        model_out = os.path.join(sep_dir, "htdemucs", base_name)
        vocals_wav = os.path.join(model_out, "vocals.wav")
        bgm_wav = os.path.join(model_out, "no_vocals.wav")

        if os.path.isfile(vocals_wav) and os.path.isfile(bgm_wav):
            print(f"  -> Tách âm thành công! Vocals: {vocals_wav} | BGM: {bgm_wav}", flush=True)
            return vocals_wav, bgm_wav

        # Quét tìm nếu tên file cấu trúc khác
        for root, _, files in os.walk(sep_dir):
            if "vocals.wav" in files and "no_vocals.wav" in files:
                found_voc = os.path.join(root, "vocals.wav")
                found_bgm = os.path.join(root, "no_vocals.wav")
                print(f"  -> Tách âm thành công (tìm thấy)! Vocals: {found_voc} | BGM: {found_bgm}", flush=True)
                return found_voc, found_bgm

        raise RuntimeError("Không tìm thấy vocals.wav và no_vocals.wav sau khi Demucs kết thúc.")
    except Exception as exc:
        print(f"  [WARN] Tách âm Demucs gặp sự cố ({exc}). Fallback: Dùng audio gốc làm BGM.", flush=True)
        return audio_path, audio_path


def format_timestamp(seconds: float) -> str:
    millis = int((seconds % 1) * 1000)
    seconds = int(seconds)
    mins, secs = divmod(seconds, 60)
    hours, mins = divmod(mins, 60)
    return f"{hours:02d}:{mins:02d}:{secs:02d},{millis:03d}"


def _merge_speech_windows(windows: list[dict], audio_samples: int, sample_rate: int = 16000) -> list[dict]:
    """Union original/vocal detections; retain short pauses and absolute source offsets."""
    merged = []
    for window in sorted(windows, key=lambda w: w["start"]):
        start, end = max(0, int(window["start"])), min(audio_samples, int(window["end"]))
        if end <= start:
            continue
        if merged and start <= merged[-1]["end"] + sample_rate:
            merged[-1]["end"] = max(merged[-1]["end"], end)
        else:
            merged.append({"start": start, "end": end})
    return merged


def _asr_speech_audio(audio_path: str, reference_audio_path: str | None = None):
    import whisper
    from faster_whisper.vad import VadOptions, get_speech_timestamps

    sample_rate = whisper.audio.SAMPLE_RATE
    audio = whisper.load_audio(audio_path)
    # Use the same bundled Silero ONNX detector and policy as Local V1.
    # Silero 6.2 JIT classified the Canary's closing music as speech; combining
    # that false positive with padded Whisper windows generated false tail cues.
    vad_options = VadOptions(threshold=0.4, min_silence_duration_ms=600)
    tracks = [("vocals", audio)]
    if reference_audio_path and reference_audio_path != audio_path:
        reference = whisper.load_audio(reference_audio_path)
        if abs(len(reference) - len(audio)) / sample_rate > 0.25:
            raise PipelineStageError("ASR_TIMING_INVALID", "Vocal và audio gốc lệch thời lượng; kiểm tra tách âm.")
        tracks.append(("original", reference))
    detections = {}
    for name, samples in tracks:
        detections[name] = get_speech_timestamps(samples, vad_options, sampling_rate=sample_rate)
    windows = _merge_speech_windows([w for rows in detections.values() for w in rows], len(audio), sample_rate)
    seconds = [{k: v / sample_rate for k, v in w.items()} for w in windows]
    return audio, {
        "vad": "faster-whisper==1.2.1/bundled-silero-onnx", "sample_rate": sample_rate,
        "audio_duration_seconds": len(audio) / sample_rate,
        "policy": {"threshold": 0.4, "min_speech_duration_ms": vad_options.min_speech_duration_ms,
                   "min_silence_duration_ms": 600, "speech_pad_ms": vad_options.speech_pad_ms,
                   "merge_gap_seconds": 1.0},
        "track_windows_seconds": {name: [{k: v / sample_rate for k, v in w.items()} for w in rows]
                                  for name, rows in detections.items()},
        "speech_windows_seconds": seconds,
        "word_timestamps": True, "condition_on_previous_text": False,
    }


def _validate_asr_alignment(raw_segments: list[dict], windows: list[dict]) -> None:
    for segment in raw_segments:
        start, end = float(segment["start"]), float(segment["end"])
        if not any(w["start"] - 0.25 <= start < end <= w["end"] + 0.25 for w in windows):
            raise PipelineStageError("ASR_TIMING_INVALID", "Mốc ASR vượt vùng có lời đã xác minh; không tự cắt câu.")
        words = [w for w in segment.get("words", []) if re.search(r"\w", str(w.get("word", "")))]
        # A sequence collapsed to a single instant is not acoustic evidence of speech.
        if len(words) >= 4 and sum(float(w["end"]) <= float(w["start"]) for w in words) > len(words) / 2:
            raise PipelineStageError("ASR_TIMING_INVALID", "Căn chỉnh ASR có đa số từ dài 0 giây; cần kiểm tra lời nhận dạng ảo.")


def _whisper_clip_timestamps(windows: list[dict], audio_samples: int) -> list[float]:
    # Whisper 20240930 floors mel length to 160-sample hops but rounds clip ends.
    # A rounded end one frame beyond content_frames causes a zero-size seek loop.
    # Match the model's actual frame domain (10 ms), without trimming audio/cues.
    content_frames = audio_samples // 160
    clips = []
    for window in windows:
        start = max(0, round(window["start"] * 100))
        end = min(content_frames, round(window["end"] * 100))
        if end <= start:
            raise PipelineStageError("ASR_TIMING_INVALID", "Vùng lời nhỏ hơn frame Whisper; không tự bỏ vùng lời.")
        clips.extend([start / 100, end / 100])
    return clips


def _asr_uncovered_speech(raw_segments: list[dict], windows: list[dict]) -> list[dict]:
    """Find long VAD-positive gaps; never publish a truncated transcript as success."""
    gaps = []
    for window in windows:
        cursor = window["start"]
        for segment in sorted(raw_segments, key=lambda s: s["start"]):
            start, end = max(window["start"], segment["start"]), min(window["end"], segment["end"])
            if end <= start:
                continue
            if start - cursor > 3.0:
                gaps.append({"start": cursor, "end": start})
            cursor = max(cursor, end)
        if window["end"] - cursor > 3.0:
            gaps.append({"start": cursor, "end": window["end"]})
    return gaps


def _asr_low_confidence(raw_segments: list[dict]) -> list[dict]:
    """Reject severely uncertain decoder output even when its timestamps fit."""
    failures = []
    for segment in raw_segments:
        score = float(segment.get("avg_logprob", 0.0))
        if not math.isfinite(score) or score < ASR_MIN_AVG_LOGPROB:
            failures.append({"start": segment["start"], "end": segment["end"], "avg_logprob": score})
    return failures


def _asr_alignment_error(raw_segments: list[dict], windows: list[dict]) -> str | None:
    try:
        _validate_asr_alignment(raw_segments, windows)
    except PipelineStageError as exc:
        return str(exc)
    return None


def step3_asr_whisper(audio_path: str, output_srt: str, *, reference_audio_path: str | None = None,
                      audit_path: str | None = None) -> list[dict]:
    log_step(3, 8, f"Nhận dạng giọng nói (ASR) bằng Whisper {WHISPER_MODEL}...")
    import torch
    import whisper

    device = "cuda" if torch.cuda.is_available() else "cpu"
    print(f"  -> Whisper sử dụng thiết bị: {device}", flush=True)
    audio, audit = _asr_speech_audio(audio_path, reference_audio_path)
    audit["model"] = WHISPER_MODEL
    # Exhausted temperature fallback returned mixed-language gibberish at T=1.0
    # on a real clip. Use beam search and retain confidence failures for retry.
    decode_options = {"verbose": False, "word_timestamps": True,
                      "condition_on_previous_text": False, "temperature": 0.0, "beam_size": 5}
    audit["decoder_policy"] = {"temperature": 0.0, "beam_size": 5, "min_avg_logprob": ASR_MIN_AVG_LOGPROB}
    windows = audit["speech_windows_seconds"]
    has_original = bool(reference_audio_path and reference_audio_path != audio_path)
    # Local V1 decodes the original. Separation can suppress short syllables or
    # introduce artifacts; reserve the separated vocal for a bounded retry.
    primary_audio = whisper.load_audio(reference_audio_path) if windows and has_original else audio
    audit["recognition_track"] = "original" if has_original else "vocals"
    clips = _whisper_clip_timestamps(windows, len(primary_audio))
    audit["decoder_clip_timestamps"] = clips
    audit_file = Path(audit_path) if audit_path else Path(output_srt).with_suffix(".asr.json")
    audit_file.write_text(json.dumps(audit, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"  -> ASR speech windows (absolute seconds): {windows}", flush=True)
    # Decoder timestamp tokens can point into the padded part of Whisper's final
    # 30-second window. DTW word alignment uses the actual audio frames and refines
    # segment boundaries; do not repair decoder timestamps by clipping/dropping cues.
    if windows:
        # Match Local V1's model family. Large-v3 dropped real speech on the
        # long Canary even after an original-track retry with valid clip bounds.
        model = whisper.load_model(WHISPER_MODEL, device=device)
        result = model.transcribe(primary_audio, clip_timestamps=clips, **decode_options)
    else:
        result = {"segments": [], "text": ""}
    # Validate both tracks identically and preserve rejected output for review.
    coverage_windows = audit.get("track_windows_seconds", {}).get("original", windows)
    gaps = _asr_uncovered_speech(result.get("segments", []), coverage_windows)
    uncertain = _asr_low_confidence(result.get("segments", []))
    alignment_error = _asr_alignment_error(result.get("segments", []), windows)
    if (gaps or uncertain or alignment_error) and has_original:
        audit["initial_uncovered_speech"] = gaps
        audit["initial_low_confidence_segments"] = uncertain
        audit["initial_alignment_error"] = alignment_error
        audit["initial_segments"] = result.get("segments", [])
        print("  -> ASR audio gốc chưa đạt; nhận dạng vocal một lần với cùng chính sách kiểm tra.", flush=True)
        clips = _whisper_clip_timestamps(windows, len(audio))
        result = model.transcribe(audio, clip_timestamps=clips, **decode_options)
        audit["recognition_track"] = "vocals_retry"
        audit["decoder_clip_timestamps"] = clips
        gaps = _asr_uncovered_speech(result.get("segments", []), coverage_windows)
        uncertain = _asr_low_confidence(result.get("segments", []))
        alignment_error = _asr_alignment_error(result.get("segments", []), windows)
    audit["uncovered_speech"] = gaps
    audit["low_confidence_segments"] = uncertain
    audit["alignment_error"] = alignment_error
    audit["segments"] = result.get("segments", [])
    audit_file.write_text(json.dumps(audit, ensure_ascii=False, indent=2), encoding="utf-8")
    if gaps:
        raise PipelineStageError("ASR_TIMING_INVALID", f"ASR bỏ trống vùng có lời {gaps}; chặn bản nhận dạng thiếu.")
    if uncertain:
        raise PipelineStageError("ASR_TIMING_INVALID", f"ASR có độ tin cậy quá thấp {uncertain}; cần kiểm tra audio nguồn.")
    if alignment_error:
        raise PipelineStageError("ASR_TIMING_INVALID", alignment_error)

    from asr_segmentation import ASRSegmentationError, split_aligned_asr_segments
    try:
        segments, segmentation_audit = split_aligned_asr_segments(result.get("segments", []))
    except ASRSegmentationError as exc:
        raise PipelineStageError("ASR_TIMING_INVALID", f"Không chia được cue theo mốc từ: {exc}") from exc
    audit["segmentation"] = segmentation_audit
    audit_file.write_text(json.dumps(audit, ensure_ascii=False, indent=2), encoding="utf-8")

    validate_asr_source_timeline(audio_path, segments)
    print(f"  -> ASR word alignment: {len(segments)} cues, end="
          f"{max((s['end'] for s in segments), default=0):.3f}s", flush=True)

    # Ghi file SRT gốc chỉ sau khi xác minh mốc trên audio thật.
    with open(output_srt, "w", encoding="utf-8") as f:
        for idx, seg in enumerate(segments, 1):
            start_ts = format_timestamp(seg["start"])
            end_ts = format_timestamp(seg["end"])
            f.write(f"{idx}\n{start_ts} --> {end_ts}\n{seg['text']}\n\n")

    print(f"  -> Đã nhận dạng được {len(segments)} câu thoại.", flush=True)
    return segments


def _cjk_only_cloud(text: str) -> str:
    return "".join(re.findall(r"[\u4e00-\u9fff\u3400-\u4dbf]", str(text or "")))


def _refine_caption_rows(reader, capture, rows: list[dict], asr_text: str) -> list[dict]:
    """Re-read weak, conflicting caption evidence without lowering correction thresholds."""
    import cv2

    refined = []
    for original in rows:
        row = dict(original)
        caption = _cjk_only_cloud(row.get("text", ""))
        source = _cjk_only_cloud(asr_text)
        if (float(row.get("prob", 0)) >= 0.85 or not caption or caption in source
                or SequenceMatcher(None, caption, source).ratio() < 0.45):
            refined.append(row)
            continue
        capture.set(cv2.CAP_PROP_POS_MSEC, float(row["sample_time"]) * 1000)
        ok, frame = capture.read()
        if not ok or frame is None:
            refined.append(row)
            continue
        height, width = frame.shape[:2]
        pad = max(8, int(height * 0.008))
        x1, x2 = max(0, int(row["x_pct"] * width) - pad), min(width, int(row["max_x_pct"] * width) + pad)
        y1, y2 = max(0, int(row["y_pct"] * height) - pad), min(height, int(row["max_y_pct"] * height) + pad)
        crop = frame[y1:y2, x1:x2]
        if not crop.size:
            refined.append(row)
            continue
        attempts = []
        for scale in (2.0, 1.5, 3.0):
            enlarged = cv2.resize(crop, None, fx=scale, fy=scale, interpolation=cv2.INTER_CUBIC)
            corroborated = False
            for _, text, probability in reader.readtext(cv2.cvtColor(enlarged, cv2.COLOR_BGR2RGB)):
                attempts.append({"text": str(text), "prob": float(probability), "scale": scale})
                # Re-reading can corroborate the original caption only. A newly
                # guessed string cannot overwrite ASR solely because it is confident.
                if _cjk_only_cloud(text) == caption and float(probability) > float(row.get("prob", 0)):
                    row["original_prob"] = original["prob"]
                    row["prob"] = float(probability)
                    row["recognition_method"] = "caption_crop_confirmation"
                    if float(probability) >= 0.80:
                        corroborated = True
            if float(row.get("prob", 0)) >= 0.85 or corroborated:
                break
        row["crop_reads"] = attempts
        refined.append(row)
    return refined


def _maybe_correct_homophone_cloud(
    seg: dict,
    selected_rows: list[dict],
    best_row: dict | None,
    seg_start: float | None = None,
    seg_end: float | None = None,
    prev_asr: str = "",
    next_asr: str = "",
) -> bool:
    asr_text = str(seg.get("text", "") or "").strip()
    asr_cjk = _cjk_only_cloud(asr_text)
    if len(asr_cjk) < 2:
        return False
    rows = list(selected_rows or [])
    if seg_start is not None and seg_end is not None and rows:
        in_cue = [
            r for r in rows
            if (seg_start + 0.04) <= float(r.get("sample_time", seg_start) or seg_start) <= (seg_end - 0.04)
        ]
        if not in_cue:
            in_cue = [
                r for r in rows
                if seg_start <= float(r.get("sample_time", seg_start) or seg_start) <= seg_end
            ]
        if in_cue:
            rows = in_cue
    prev_cjk_seg = _cjk_only_cloud(prev_asr)
    next_cjk_seg = _cjk_only_cloud(next_asr)
    # Whole-line confidence penalizes a single ambiguous glyph. Independent
    # frames may corroborate a small substitution without inventing confidence.
    # This path cannot add/delete text or replace an unrelated caption.
    consensus = {}
    for row in rows:
        txt = str(row.get("text", "")).strip()
        cjk = _cjk_only_cloud(txt)
        prob = float(row.get("prob", 0))
        if (prob < 0.55 or len(cjk) < 6 or len(cjk) != len(asr_cjk)
                or re.findall(r"\d+", txt) != re.findall(r"\d+", asr_text)):
            continue
        substitutions = sum(a != b for a, b in zip(cjk, asr_cjk))
        if not (1 <= substitutions <= 2 and substitutions / len(cjk) <= 0.25):
            continue
        if any(adj and SequenceMatcher(None, cjk, adj).ratio() > SequenceMatcher(None, cjk, asr_cjk).ratio()
               for adj in (prev_cjk_seg, next_cjk_seg)):
            continue
        consensus.setdefault(cjk, []).append(row)
    supported = []
    for cjk, evidence in consensus.items():
        times = sorted({round(float(r.get("sample_time", 0)), 3) for r in evidence})
        if len(times) >= 3 and times[-1] - times[0] >= 0.25 and max(float(r["prob"]) for r in evidence) >= 0.75:
            supported.append((cjk, evidence))
    if len(supported) == 1:
        cjk, evidence = supported[0]
        seg["ocr_correction"] = {"method": "three_frame_small_substitution", "asr_text": asr_text,
            "caption_text": cjk, "samples": [{"time": r["sample_time"], "prob": r["prob"]} for r in evidence]}
        seg["text"] = max(evidence, key=lambda r: float(r["prob"]))["text"]
        return True
    ordered_texts = []
    ordered_probs = []
    ordered_times = []
    for row in rows:
        prob = float(row.get("prob", 0.0) or 0.0)
        txt = str(row.get("text", "") or "").strip()
        txt_cjk = _cjk_only_cloud(txt)
        if prob < 0.80 or len(txt_cjk) < 2:
            continue
        if len(rows) > 1:
            adj_match = (
                (prev_cjk_seg and SequenceMatcher(None, txt_cjk, prev_cjk_seg).ratio() >= 0.65)
                or (next_cjk_seg and SequenceMatcher(None, txt_cjk, next_cjk_seg).ratio() >= 0.65)
            )
            own_match = SequenceMatcher(None, txt_cjk, asr_cjk).ratio()
            if adj_match and own_match < 0.65:
                continue
        row_t = float(row.get("sample_time", 0.0) or 0.0)
        if not ordered_texts:
            ordered_texts.append(txt)
            ordered_probs.append(prob)
            ordered_times.append(row_t)
        else:
            prev_cjk = _cjk_only_cloud(ordered_texts[-1])
            if txt_cjk == prev_cjk:
                ordered_probs[-1] = max(ordered_probs[-1], prob)
                continue
            if prev_cjk in txt_cjk:
                ordered_texts[-1] = txt
                ordered_probs[-1] = max(ordered_probs[-1], prob)
            elif txt_cjk in prev_cjk:
                ordered_probs[-1] = max(ordered_probs[-1], prob)
                continue
            elif SequenceMatcher(None, txt_cjk, prev_cjk).ratio() >= 0.75:
                if prob > ordered_probs[-1]:
                    ordered_texts[-1] = txt
                    ordered_probs[-1] = prob
                continue
            else:
                ordered_texts.append(txt)
                ordered_probs.append(prob)
                ordered_times.append(row_t)
    if not ordered_texts and best_row:
        prob = float(best_row.get("prob", 0.0) or 0.0)
        txt = str(best_row.get("text", "") or "").strip()
        if prob >= 0.80 and len(_cjk_only_cloud(txt)) >= 2:
            ordered_texts.append(txt)
            ordered_probs.append(prob)
            ordered_times.append(float(best_row.get("sample_time", 0.0) or 0.0))
    if not ordered_texts:
        return False
    avg_prob = sum(ordered_probs) / len(ordered_probs)
    if avg_prob < 0.85:
        return False
    combined_ocr = "，".join(ordered_texts)
    ocr_cjk = _cjk_only_cloud(combined_ocr)
    if not ocr_cjk or ocr_cjk == asr_cjk:
        return False
    if len(ocr_cjk) > len(asr_cjk) * 1.35 + 1:
        target_len = len(asr_cjk)
        if len(ordered_texts) == 1 and len(ocr_cjk) >= target_len:
            prefix_cjk = ocr_cjk[:target_len]
            suffix_cjk = ocr_cjk[-target_len:]
            p_sim = SequenceMatcher(None, asr_cjk, prefix_cjk).ratio()
            s_sim = SequenceMatcher(None, asr_cjk, suffix_cjk).ratio()
            if max(p_sim, s_sim) < 0.50:
                return False
            combined_ocr = prefix_cjk if p_sim >= s_sim else suffix_cjk
            ocr_cjk = combined_ocr
        elif len(ordered_texts) >= 2:
            cue_mid = ((seg_start + seg_end) * 0.5) if (seg_start is not None and seg_end is not None) else ordered_times[0]
            within = [
                (abs(ordered_times[i] - cue_mid), i, t)
                for i, t in enumerate(ordered_texts)
                if len(_cjk_only_cloud(t)) <= len(asr_cjk) * 1.35 + 1
            ]
            if not within:
                return False
            _, _, best_single = min(within)
            combined_ocr = best_single
            ocr_cjk = _cjk_only_cloud(combined_ocr)
            if not ocr_cjk or ocr_cjk == asr_cjk:
                return False
    if ocr_cjk in asr_cjk and len(ocr_cjk) < len(asr_cjk):
        return False
    if len(ocr_cjk) < len(asr_cjk) * 0.72 and asr_cjk not in ocr_cjk:
        return False
    # A caption sampled after a short spoken prefix may only show the next
    # clause. High confidence for that caption does not authorize deleting the
    # prefix (especially after ASR coalesced a short cue with its neighbor).
    edits = SequenceMatcher(None, asr_cjk, ocr_cjk).get_opcodes()
    if any(tag in ("delete", "replace") and (a2 - a1) - (b2 - b1) >= 2
           for tag, a1, a2, b1, b2 in edits):
        return False
    it = iter(asr_cjk)
    if len(ocr_cjk) <= len(asr_cjk) and all(ch in it for ch in ocr_cjk):
        return False
    sim = SequenceMatcher(None, asr_cjk, ocr_cjk).ratio()
    if 0.60 <= sim < 0.99:
        print(f"  [OCR Fix] '{asr_text}' -> '{combined_ocr}' (sim={sim:.2f}, prob={avg_prob:.2f})", flush=True)
        seg["text"] = combined_ocr
        seg["ocr_correction"] = {"method": "confident_caption", "asr_text": asr_text,
                                  "caption_text": combined_ocr, "prob": avg_prob}
        return True
    return False


def validate_asr_source_timeline(input_video: str, segments: list[dict]) -> float:
    """Use measured source duration; ASR timestamps cannot extend the source video."""
    try:
        probe = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "json", input_video],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30, shell=False,
        )
        if probe.returncode:
            raise ValueError("ffprobe rejected source video")
        duration = float(json.loads(probe.stdout)["format"]["duration"])
        if not math.isfinite(duration) or duration <= 0:
            raise ValueError("source duration must be positive and finite")
    except (ValueError, TypeError, KeyError, OSError, subprocess.TimeoutExpired) as exc:
        raise PipelineStageError("ASR_TIMING_INVALID", f"Không đo được thời lượng video nguồn: {exc}") from exc
    for segment in segments:
        try:
            start, end = float(segment["start"]), float(segment["end"])
            valid = (math.isfinite(start) and math.isfinite(end) and 0 <= start < duration
                     and start < end <= duration + 0.25)
        except (KeyError, TypeError, ValueError):
            valid = False
        if not valid:
            raise PipelineStageError(
                "ASR_TIMING_INVALID",
                f"Cue #{segment.get('id')} có mốc ASR ngoài nguồn ({duration:.3f}s): "
                f"{segment.get('start')} -> {segment.get('end')}. Chặn trước dịch/TTS; không tự bỏ câu.",
            )
    return duration


def step3b_ocr_and_fix_subtitles(
    input_video: str,
    segments: list[dict],
    output_srt: str = "subtitles.srt",
) -> tuple[list[dict], float, int, int, float]:
    """
    Quét EasyOCR GPU để định vị chính xác vùng phụ đề gốc và tự sửa lỗi đồng âm Whisper ASR.
    Trả về (segments, main_y_pct, video_w, video_h, video_duration).
    """
    print("\n[3b/8] Quét OCR GPU định vị hộp che phụ đề & chuẩn hóa chữ Trung...", flush=True)
    _write_pipeline_progress(64, 3, 8, "Quét OCR GPU định vị hộp che phụ đề & chuẩn hóa chữ Trung...")
    video_w, video_h = 1080, 1920
    video_duration = validate_asr_source_timeline(input_video, segments)
    main_y_pct = 0.78

    try:
        import cv2
        import torch
        import easyocr
        from ocr_subtitle_locator import select_chinese_subtitle_band
        from ass_utils import _merge_contiguous_tracking_blocks

        cap = cv2.VideoCapture(input_video)
        if not cap.isOpened():
            raise RuntimeError(f"Không mở được video bằng OpenCV: {input_video}")
        video_w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH) or 1080)
        video_h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT) or 1920)
        fps = float(cap.get(cv2.CAP_PROP_FPS) or 30.0)
        frame_count = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
        # Keep ffprobe's source duration; frame estimates and ASR cannot inflate it.

        use_gpu = bool(torch.cuda.is_available())
        print(f"  -> EasyOCR GPU={use_gpu} | Kích thước video: {video_w}x{video_h} ({video_duration:.2f}s)", flush=True)
        reader = easyocr.Reader(["ch_sim", "en"], gpu=use_gpu, verbose=False)

        can_grab = callable(getattr(cap, "grab", None)) and fps > 0 and frame_count > 0
        cur_pos_frame: list[int | None] = [0 if can_grab else None]
        max_grab_span = max(45, int(fps * 3.0)) if fps > 0 else 45

        def _read_frame_at_sec(t_sec: float):
            if can_grab:
                target_idx = min(max(0, int(round(t_sec * fps))), max(0, frame_count - 1))
                if cur_pos_frame[0] is not None and 0 <= (target_idx - cur_pos_frame[0]) <= max_grab_span:
                    steps = target_idx - cur_pos_frame[0]
                    step_ok = True
                    for _ in range(steps):
                        if not cap.grab():
                            step_ok = False
                            break
                    if step_ok:
                        ok_f, frm = cap.read()
                        if ok_f and frm is not None:
                            cur_pos_frame[0] = target_idx + 1
                            return True, frm
                cur_pos_frame[0] = None
                cap.set(cv2.CAP_PROP_POS_MSEC, t_sec * 1000.0)
                ok_f, frm = cap.read()
                if ok_f and frm is not None:
                    pos_after = cap.get(cv2.CAP_PROP_POS_FRAMES)
                    if isinstance(pos_after, (int, float)) and math.isfinite(pos_after) and pos_after >= 0:
                        cur_pos_frame[0] = int(round(pos_after))
                return ok_f, frm
            cap.set(cv2.CAP_PROP_POS_MSEC, t_sec * 1000.0)
            return cap.read()

        all_blocks = []
        roi_bounds: tuple[float, float] | None = None
        anchor_probe_limit = 10

        for seg_idx, s in enumerate(segments):
            s_id = s["id"]
            st = float(s["start"])
            et = float(s["end"])
            dur = max(0.1, et - st)
            if dur <= 0.8:
                sample_times = [st + dur * 0.5]
            elif dur <= 2.0:
                sample_times = [st + dur * fraction for fraction in (0.25, 0.5, 0.75)]
            else:
                n_samp = min(4, max(2, int(dur / 0.8)))
                sample_times = [st + (i + 0.5) * dur / n_samp for i in range(n_samp)]

            if seg_idx == anchor_probe_limit and len(segments) > 12 and all_blocks:
                probe_band = select_chinese_subtitle_band(
                    all_blocks,
                    {seg_i["id"]: seg_i["text"] for seg_i in segments[:anchor_probe_limit]},
                    frame_width=video_w,
                    frame_height=video_h,
                    duration=video_duration,
                )
                if getattr(probe_band, "support", 0) >= 5:
                    roi_bounds = (
                        max(0.0, float(probe_band.top) - 0.12),
                        min(1.0, float(probe_band.bottom) + 0.12),
                    )

            for t in sample_times:
                ok, frame = _read_frame_at_sec(t)
                if not ok or frame is None:
                    continue
                fh, fw = frame.shape[:2]
                max_dim = max(fw, fh)
                if max_dim > 1280:
                    scale = 1280.0 / max_dim
                    frame = cv2.resize(frame, (int(fw * scale), int(fh * scale)), interpolation=cv2.INTER_AREA)
                    fh, fw = frame.shape[:2]
                rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)

                used_y_offset = 0
                detections = []
                if roi_bounds is not None:
                    ry0 = max(0, int(roi_bounds[0] * fh))
                    ry1 = min(fh, max(ry0 + 16, math.ceil(roi_bounds[1] * fh)))
                    roi_crop = rgb[ry0:ry1]
                    if roi_crop.size > 0:
                        roi_dets = reader.readtext(roi_crop)
                        if any(len(_cjk_only_cloud(txt)) >= 2 for _, txt, _ in roi_dets):
                            detections = roi_dets
                            used_y_offset = ry0

                if not detections:
                    detections = reader.readtext(rgb)
                    used_y_offset = 0

                for bbox, txt, prob in detections:
                    if not txt or len(bbox) < 4:
                        continue
                    xs = [p[0] for p in bbox]
                    ys = [p[1] + used_y_offset for p in bbox]
                    x1 = max(0.0, min(1.0, float(min(xs)) / fw))
                    x2 = max(0.0, min(1.0, float(max(xs)) / fw))
                    y1 = max(0.0, min(1.0, float(min(ys)) / fh))
                    y2 = max(0.0, min(1.0, float(max(ys)) / fh))
                    all_blocks.append(SimpleNamespace(
                        text=str(txt).strip(),
                        x_pct=x1,
                        max_x_pct=x2,
                        y_pct=y1,
                        max_y_pct=y2,
                        prob=float(prob),
                        sample_segment_id=s_id,
                        sample_time=float(t),
                        start=st,
                        end=et,
                        is_subtitle=True,
                        is_packaging=False,
                    ))
        cap.release()

        seg_texts = {s["id"]: s["text"] for s in segments}
        band = select_chinese_subtitle_band(
            all_blocks,
            seg_texts,
            frame_width=video_w,
            frame_height=video_h,
            duration=video_duration,
        )

        main_y_pct = float(band.top) if getattr(band, "support", 0) >= 1 else 0.78
        band_bottom = float(band.bottom) if getattr(band, "support", 0) >= 1 else min(0.95, main_y_pct + 0.06)
        band_h_limit = max(0.08, (band_bottom - main_y_pct) * 1.8)
        ocr_fixed = 0
        located_count = 0
        ocr_audit = []
        refinement_cap = cv2.VideoCapture(input_video)

        for idx, s in enumerate(segments):
            s_id = s["id"]
            seg_s = float(s["start"])
            next_start = float(segments[idx + 1]["start"]) if idx + 1 < len(segments) else video_duration
            seg_e = min(float(s["end"]) + 0.4, next_start) if next_start > float(s["end"]) else (float(s["end"]) + 0.3)
            if video_duration > 0:
                seg_e = min(seg_e, video_duration)

            b = band.selected_by_segment.get(s_id)
            s["y_pct"] = main_y_pct
            s["max_y_pct"] = band_bottom

            if b:
                located_count += 1
                selected = sorted(band.selected_by_sample.get(s_id, []), key=lambda r: float(r.get("sample_time", 0.0)))
                selected = _refine_caption_rows(reader, refinement_cap, selected, s["text"])
                s["ocr_caption_rows"] = selected
                ocr_audit.append({"id": s_id, "asr_text": s["text"], "selected_rows": selected})
                tracking = []
                for pos, row in enumerate(selected):
                    left = seg_s if pos == 0 else (selected[pos - 1]["sample_time"] + row["sample_time"]) * 0.5
                    right = seg_e if pos == len(selected) - 1 else (row["sample_time"] + selected[pos + 1]["sample_time"]) * 0.5
                    if right > left:
                        tracking.append({
                            "text": row["text"],
                            "start": max(seg_s, left),
                            "end": min(seg_e, right),
                            "x_pct": row["x_pct"],
                            "max_x_pct": row["max_x_pct"],
                            "y_pct": row["y_pct"],
                            "max_y_pct": row["max_y_pct"],
                            "prob": float(row.get("prob", 0.9)),
                        })
                tracking = _merge_contiguous_tracking_blocks(tracking, seg_s=seg_s, seg_e=seg_e)
                s["tracking_blocks"] = tracking
                box_x1 = min([float(b["x_pct"])] + [float(r["x_pct"]) for r in selected]) if selected else float(b["x_pct"])
                box_x2 = max([float(b["max_x_pct"])] + [float(r["max_x_pct"]) for r in selected]) if selected else float(b["max_x_pct"])
                s["best_block"] = {
                    "text": b["text"],
                    "start": seg_s,
                    "end": seg_e,
                    "x_pct": box_x1,
                    "max_x_pct": box_x2,
                    "y_pct": b["y_pct"],
                    "max_y_pct": b["max_y_pct"],
                    "prob": float(b.get("prob", 1.0)),
                }
                s["y_pct"] = float(b["y_pct"])
                s["max_y_pct"] = float(b["max_y_pct"])
                s["ocr_mask_status"] = "located"
                prev_asr_txt = str(segments[idx - 1].get("text", "") or "") if idx > 0 else ""
                next_asr_txt = str(segments[idx + 1].get("text", "") or "") if idx + 1 < len(segments) else ""
                if _maybe_correct_homophone_cloud(
                    s,
                    selected,
                    b,
                    seg_start=float(s["start"]),
                    seg_end=float(s["end"]),
                    prev_asr=prev_asr_txt,
                    next_asr=next_asr_txt,
                ):
                    ocr_fixed += 1
            else:
                band_hits = [
                    blk for blk in all_blocks
                    if blk.sample_segment_id == s_id
                    and float(blk.prob) >= 0.70
                    and abs((blk.x_pct + blk.max_x_pct) * 0.5 - 0.5) <= 0.15
                    and (blk.max_x_pct - blk.x_pct) >= 0.05
                    and (blk.max_y_pct - blk.y_pct) <= band_h_limit
                    and blk.y_pct >= main_y_pct - 0.025
                    and blk.max_y_pct <= band_bottom + 0.025
                    and len(_cjk_only_cloud(blk.text)) >= 2
                ]
                if band_hits:
                    located_count += 1
                    sorted_hits = sorted(band_hits, key=lambda item: item.sample_time)
                    s["ocr_caption_rows"] = [{"text": item.text, "prob": item.prob, "sample_time": item.sample_time}
                                             for item in sorted_hits]
                    hx1 = min(blk.x_pct for blk in sorted_hits)
                    hx2 = max(blk.max_x_pct for blk in sorted_hits)
                    hy1 = min(blk.y_pct for blk in sorted_hits)
                    hy2 = max(blk.max_y_pct for blk in sorted_hits)
                    s["best_block"] = {
                        "text": "，".join(dict.fromkeys(blk.text for blk in sorted_hits if blk.text)),
                        "start": seg_s,
                        "end": seg_e,
                        "x_pct": hx1,
                        "max_x_pct": hx2,
                        "y_pct": hy1,
                        "max_y_pct": hy2,
                        "prob": max(blk.prob for blk in sorted_hits),
                    }
                    s["tracking_blocks"] = []
                    s["y_pct"] = hy1
                    s["max_y_pct"] = hy2
                    s["ocr_mask_status"] = "located"
                    prev_asr_txt = str(segments[idx - 1].get("text", "") or "") if idx > 0 else ""
                    next_asr_txt = str(segments[idx + 1].get("text", "") or "") if idx + 1 < len(segments) else ""
                    if _maybe_correct_homophone_cloud(
                        s,
                        [{"text": b_i.text, "prob": b_i.prob, "sample_time": b_i.sample_time} for b_i in sorted_hits],
                        None,
                        seg_start=float(s["start"]),
                        seg_end=float(s["end"]),
                        prev_asr=prev_asr_txt,
                        next_asr=next_asr_txt,
                    ):
                        ocr_fixed += 1
                else:
                    s["best_block"] = None
                    s["tracking_blocks"] = []
                    s["ocr_mask_status"] = "no_caption_evidence"

        refinement_cap.release()
        _write_pipeline_progress(66, 3, 8, "Đối soát tọa độ hộp che phụ đề gốc đa tỷ lệ...")
        # A recognizer's confidence measures text, not the detector's geometry.
        # Reacquire source boxes (including speech gaps) before translation/TTS.
        from caption_geometry import acquire_caption_geometry, validate_caption_evidence
        evidence = acquire_caption_geometry(
            input_video, segments, reader,
            band_top=main_y_pct if getattr(band, "support", 0) >= 1 else None,
            band_bottom=band_bottom if getattr(band, "support", 0) >= 1 else None,
            duration=video_duration,
            audit_path=Path(output_srt).with_name("caption_geometry.json"),
        )
        validate_caption_evidence(evidence, cue_count=len(segments))
        located_count = sum(s.get("ocr_mask_status") == "located" for s in segments)
        by_id = {s["id"]: s for s in segments}
        for row in ocr_audit:
            row["final_text"] = by_id[row["id"]]["text"]
        Path(output_srt).with_suffix(".ocr.json").write_text(
            json.dumps({"rows": ocr_audit, "segments": segments}, ensure_ascii=False, indent=2), encoding="utf-8")
        if ocr_fixed > 0:
            with open(output_srt, "w", encoding="utf-8") as f:
                for idx, seg in enumerate(segments, 1):
                    f.write(f"{idx}\n{format_timestamp(seg['start'])} --> {format_timestamp(seg['end'])}\n{seg['text']}\n\n")

        print(f"  -> OCR xong: {located_count}/{len(segments)} câu có hộp phụ đề (Y={main_y_pct:.3f}), đã sửa {ocr_fixed} lỗi đồng âm.", flush=True)
    except Exception as exc:
        raise PipelineStageError("SUBTITLE_BUILD_FAILED",
                                 f"Source-caption geometry requires review: {exc}") from exc

    return segments, main_y_pct, video_w, video_h, video_duration


_VI_LETTER_RE = re.compile(r"[A-Za-zÀ-ỹ]")


def _strip_json_fences(raw_text: str) -> str:
    text = str(raw_text or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.IGNORECASE)
        text = re.sub(r"\s*```$", "", text)
    return text.strip()


def validate_translation_items(segments: list[dict], translated_data: object) -> dict[int, str]:
    """
    Kiểm tra nghiêm ngặt schema, đủ toàn bộ cue ID, không trùng/lạ ID,
    không rỗng và không còn nguyên chữ nguồn tiếng Trung chưa dịch.
    """
    if not isinstance(translated_data, list):
        raise PipelineStageError(
            "TRANSLATION_FAILED",
            f"Phản hồi dịch không phải danh sách JSON (nhận {type(translated_data).__name__}).",
        )

    expected_by_id: dict[int, str] = {}
    for seg in segments:
        expected_by_id[int(seg["id"])] = str(seg.get("text") or "").strip()

    trans_map: dict[int, str] = {}
    for idx, item in enumerate(translated_data):
        if not isinstance(item, dict):
            raise PipelineStageError(
                "TRANSLATION_FAILED",
                f"Mục dịch #{idx} không đúng định dạng dict.",
            )
        raw_id = item.get("id")
        if isinstance(raw_id, bool) or raw_id is None:
            raise PipelineStageError("TRANSLATION_FAILED", f"Mục dịch #{idx} thiếu cue ID hợp lệ.")
        try:
            cue_id = int(raw_id)
        except (TypeError, ValueError) as exc:
            raise PipelineStageError("TRANSLATION_FAILED", f"Cue ID không hợp lệ: {raw_id!r}") from exc

        if cue_id not in expected_by_id:
            raise PipelineStageError(
                "TRANSLATION_FAILED",
                f"Bản dịch chứa cue ID lạ #{cue_id} không thuộc đầu vào.",
            )
        if cue_id in trans_map:
            raise PipelineStageError(
                "TRANSLATION_FAILED",
                f"Bản dịch bị trùng cue ID #{cue_id}.",
            )

        raw_text = item.get("text")
        if not isinstance(raw_text, str):
            raise PipelineStageError(
                "TRANSLATION_FAILED",
                f"Nội dung dịch của cue #{cue_id} không phải chuỗi hợp lệ.",
            )
        clean_text = raw_text.strip()
        if not clean_text:
            raise PipelineStageError(
                "TRANSLATION_FAILED",
                f"Bản dịch của cue #{cue_id} bị rỗng.",
            )

        src_text = expected_by_id[cue_id]
        if _cjk_only_cloud(src_text):
            if clean_text == src_text:
                raise PipelineStageError(
                    "TRANSLATION_FAILED",
                    f"Cue #{cue_id} vẫn giữ nguyên nguyên văn tiếng Trung.",
                )
            if _cjk_only_cloud(clean_text):
                raise PipelineStageError(
                    "TRANSLATION_FAILED",
                    f"Cue #{cue_id} còn sót ký tự nguồn CJK sau khi dịch.",
                )
            if not _VI_LETTER_RE.search(clean_text):
                raise PipelineStageError(
                    "TRANSLATION_FAILED",
                    f"Cue #{cue_id} không chứa chữ cái tiếng Việt/Latin hợp lệ.",
                )

        from translation_guards import TranslationMeaningError, validate_monetary_meaning
        try:
            validate_monetary_meaning(src_text, clean_text)
        except TranslationMeaningError as exc:
            raise PipelineStageError("TRANSLATION_FAILED", f"Cue #{cue_id}: {exc}") from exc
        trans_map[cue_id] = clean_text

    missing_ids = [sid for sid in expected_by_id if sid not in trans_map]
    if missing_ids:
        raise PipelineStageError(
            "TRANSLATION_FAILED",
            f"Thiếu bản dịch cho {len(missing_ids)} cue: {missing_ids[:10]}",
        )
    return trans_map


def _load_audio_slice_16k(
    audio_path: str | Path,
    start_sec: float,
    end_sec: float,
    target_sr: int = 16000,
):
    """Đọc lát âm thanh mono 16kHz cho nhận diện cao độ F0 (không cần librosa/parselmouth)."""
    if not audio_path or not os.path.isfile(str(audio_path)):
        return None
    import numpy as np

    start_s = max(0.0, float(start_sec))
    dur_s = max(0.1, float(end_sec) - start_s)
    try:
        import soundfile as sf

        info = sf.info(str(audio_path))
        sr = int(info.samplerate)
        if sr <= 0:
            return None
        start_frame = max(0, int(round(start_s * sr)))
        frames = max(1, int(round(dur_s * sr)))
        data, sr = sf.read(str(audio_path), start=start_frame, frames=frames, dtype="float32", always_2d=True)
        if data.size == 0:
            return None
        y = np.mean(data, axis=1, dtype=np.float32)
        if sr != target_sr and len(y) > 8:
            from scipy.signal import resample_poly

            g = math.gcd(target_sr, sr)
            y = resample_poly(y, target_sr // g, sr // g).astype(np.float32)
            sr = target_sr
        return (y, sr) if len(y) >= int(sr * 0.1) else None
    except Exception:
        return None


def estimate_f0_pitch_cloud(y, sr: int = 16000) -> tuple[float, int, float, str]:
    """
    Ước lượng cao độ cơ bản F0 bằng thuật toán YIN chuẩn hóa (CMNDF) + lọc độ phẳng phổ,
    áp dụng đúng ranh giới phân loại Nam/Nữ của backend/ai/v1_auto_voice.py.
    Trả về (median_f0, voiced_count, confidence, gender).
    """
    import numpy as np

    if y is None or sr <= 0 or len(y) < int(sr * 0.1):
        return 0.0, 0, 0.0, "unknown"

    arr = np.asarray(y, dtype=np.float64)
    arr = arr - float(np.mean(arr))
    peak = float(np.max(np.abs(arr)))
    if not math.isfinite(peak) or peak < 1e-5:
        return 0.0, 0, 0.0, "unknown"
    arr = arr / peak

    win_len = max(64, int(round(0.04 * sr)))   # 40ms
    hop_len = max(16, int(round(0.01 * sr)))   # 10ms -> 12 frames = 120ms
    tau_min = max(2, int(sr // 350))
    tau_max = max(tau_min + 2, int(math.ceil(sr / 75.0)))
    frame_span = win_len + tau_max
    if len(arr) < frame_span:
        return 0.0, 0, 0.0, "unknown"

    n_fft = 1 << (frame_span - 1).bit_length()
    starts = range(0, len(arr) - frame_span + 1, hop_len)
    rms_list = [float(np.sqrt(np.mean(arr[s : s + win_len] ** 2))) for s in starts]
    if not rms_list:
        return 0.0, 0, 0.0, "unknown"
    rms_gate = max(0.015, float(np.percentile(rms_list, 90)) * 0.12)

    voiced_f0: list[float] = []
    taus = np.arange(1, tau_max + 1, dtype=np.float64)
    hann = np.hanning(win_len)

    for s_idx, rms in zip(starts, rms_list):
        if rms < rms_gate:
            continue
        seg = arr[s_idx : s_idx + frame_span]
        w_seg = seg[:win_len]
        spec = np.abs(np.fft.rfft(w_seg * hann)) ** 2 + 1e-12
        flatness = float(np.exp(np.mean(np.log(spec))) / np.mean(spec))
        if flatness > 0.38:
            continue

        csum = np.concatenate(([0.0], np.cumsum(seg * seg)))
        e0 = csum[win_len] - csum[0]
        e_tau = csum[win_len + 1 : win_len + tau_max + 1] - csum[1 : tau_max + 1]
        fa = np.fft.rfft(w_seg, n=n_fft)
        fb = np.fft.rfft(seg, n=n_fft)
        corr = np.fft.irfft(np.conj(fa) * fb, n=n_fft)[1 : tau_max + 1]
        diff = np.maximum(0.0, e0 + e_tau - 2.0 * corr)
        cum_mean = np.cumsum(diff) / taus
        cmndf = np.where(cum_mean > 1e-12, diff / cum_mean, 1.0)

        search = cmndf[tau_min - 1 : tau_max]
        below = np.where(search < 0.22)[0]
        if below.size > 0:
            rel = int(below[0])
            while rel + 1 < len(search) and search[rel + 1] <= search[rel]:
                rel += 1
            best_idx = (tau_min - 1) + rel
        else:
            rel = int(np.argmin(search))
            if float(search[rel]) >= 0.15:
                continue
            best_idx = (tau_min - 1) + rel

        tau_est = float(best_idx + 1)
        if 0 < best_idx < len(cmndf) - 1:
            y0, y1, y2 = float(cmndf[best_idx - 1]), float(cmndf[best_idx]), float(cmndf[best_idx + 1])
            denom = 2.0 * (2.0 * y1 - y2 - y0)
            if abs(denom) > 1e-12:
                delta = max(-0.5, min(0.5, (y2 - y0) / denom))
                tau_est += delta
        f0 = float(sr) / tau_est
        if 75.0 <= f0 <= 340.0:
            voiced_f0.append(f0)

    if len(voiced_f0) < MIN_VOICED_FRAMES:
        return 0.0, len(voiced_f0), 0.0, "unknown"

    median_f0 = float(np.median(voiced_f0))
    voiced_count = len(voiced_f0)
    if median_f0 < 175.0:
        gender = "male"
        confidence = min(0.98, 0.80 + max(0.0, 165.0 - median_f0) * 0.005)
    elif median_f0 >= 185.0:
        gender = "female"
        confidence = min(0.98, 0.80 + max(0.0, median_f0 - 195.0) * 0.005)
    else:
        gender = "male" if median_f0 < PITCH_BOUNDARY_HZ else "female"
        confidence = 0.55

    frame_scale = min(1.0, voiced_count / 30.0)
    confidence = float(max(0.0, min(1.0, confidence * frame_scale)))
    return round(median_f0, 2), voiced_count, round(confidence, 4), gender


def detect_first_speaker_gender_cloud(
    vocals_path: str | Path | None,
    segments: list[dict],
    backup_audio_path: str | Path | None = None,
) -> tuple[str, float, float, int]:
    """
    Nhận diện giới tính DUY NHẤT dựa trên câu thoại đầu tiên có lời trong video
    (khớp 100% nguyên tắc Codex Plan của v1_auto_voice.py: không nhảy sang câu thứ 2/3).
    """
    first_seg = next(
        (s for s in (segments or []) if re.search(r"\w", str(s.get("text") or ""))),
        None,
    )
    if first_seg is None:
        return "unknown", 0.0, 0.0, -1

    seg_id = int(first_seg.get("id", 1))
    start_s = float(first_seg.get("start", 0.0))
    end_s = float(first_seg.get("end", start_s + 0.5))
    best = ("unknown", 0.0, 0.0, seg_id)

    for candidate_audio in (vocals_path, backup_audio_path):
        if not candidate_audio:
            continue
        sliced = _load_audio_slice_16k(candidate_audio, start_s, end_s)
        if sliced is None:
            continue
        y, sr = sliced
        f0, _cnt, conf, gender = estimate_f0_pitch_cloud(y, sr)
        if conf > best[1]:
            best = (gender, conf, f0, seg_id)
        if best[1] >= CONFIDENCE_THRESHOLD and best[0] in ("male", "female"):
            break

    gen, conf, f0, _ = best
    if conf >= CONFIDENCE_THRESHOLD and gen in ("male", "female"):
        return gen, conf, f0, seg_id
    return "unknown", conf, f0, seg_id


def _voice_id_to_param(raw_voice_id: str, fallback: str) -> str:
    cleaned = str(raw_voice_id or "").strip()
    if cleaned.startswith("capcut-"):
        cleaned = cleaned[len("capcut-") :]
    return cleaned if cleaned else fallback


def resolve_cloud_voice_choice(
    requested_voice: str,
    segments: list[dict],
    *,
    vocals_path: str | Path | None = None,
    original_audio_path: str | Path | None = None,
    runtime_profile: dict | None = None,
    lock_path: str | Path | None = "voice_lock.json",
) -> dict:
    """
    Quyết định và khóa giọng đọc cho toàn bộ video chuẩn Tool V1:
    - requested_voice == 'auto': Đo F0 câu đầu tiên -> Nam (<180Hz) khóa BV075_streaming,
      Nữ (>=180Hz) khóa BV562_streaming, không chắc -> fallback BV562_streaming.
    - requested_voice cụ thể: Khóa trực tiếp giọng người dùng chọn, suy ra nhãn giới tính cho bước dịch.
    """
    req = str(requested_voice or "auto").strip()
    auto_cfg = (runtime_profile or {}).get("auto_voice") or {}
    female_param = _voice_id_to_param(auto_cfg.get("female_voice_id", ""), VOICE_FEMALE_PARAM)
    male_param = _voice_id_to_param(auto_cfg.get("male_voice_id", ""), VOICE_MALE_PARAM)

    if req.lower() == "auto":
        if not segments:
            decision = {
                "voice_mode": "auto",
                "requested_voice": "auto",
                "resolved_voice": female_param,
                "detected_gender": "unknown",
                "confidence": 0.0,
                "median_f0": 0.0,
                "first_segment_id": -1,
                "rule": "no_speech_default_female",
            }
        else:
            gender, conf, f0, seg_id = detect_first_speaker_gender_cloud(
                vocals_path, segments, backup_audio_path=original_audio_path
            )
            if gender == "male" and conf >= CONFIDENCE_THRESHOLD:
                resolved = male_param
                rule = "first_speaker_male_capcut"
            elif gender == "female" and conf >= CONFIDENCE_THRESHOLD:
                resolved = female_param
                rule = "first_speaker_female_capcut"
            else:
                resolved = female_param
                rule = "fallback_default_female_unknown_first_speaker"
            decision = {
                "voice_mode": "auto",
                "requested_voice": "auto",
                "resolved_voice": resolved,
                "detected_gender": gender,
                "confidence": conf,
                "median_f0": f0,
                "first_segment_id": seg_id,
                "rule": rule,
            }
        print(
            f"  -> [Auto Voice V1] Câu đầu #{decision['first_segment_id']}: gender={decision['detected_gender']} "
            f"(F0={decision['median_f0']:.1f}Hz, conf={decision['confidence']:.2f}) -> Khóa giọng: {decision['resolved_voice']}",
            flush=True,
        )
    else:
        low_req = req.lower()
        if "075" in low_req or "namminh" in low_req:
            inferred_gender = "male"
        elif "562" in low_req or "421" in low_req or "hoaimy" in low_req:
            inferred_gender = "female"
        else:
            inferred_gender = "unknown"
        decision = {
            "voice_mode": "manual",
            "requested_voice": req,
            "resolved_voice": req,
            "detected_gender": inferred_gender,
            "confidence": 1.0,
            "median_f0": 0.0,
            "first_segment_id": int(segments[0]["id"]) if segments else 0,
            "rule": "manual_selection",
        }

    if lock_path:
        try:
            Path(lock_path).write_text(json.dumps(decision, ensure_ascii=False, indent=2), encoding="utf-8")
        except OSError:
            pass
    return decision


def _extract_keyframes_jpeg_bytes(video_path: str | None, num_frames: int = 3) -> list[bytes]:
    """Trích nhanh tối đa 3 khung hình 720p phục vụ dịch ngữ cảnh trực quan như Local V1."""
    if not video_path or not os.path.isfile(video_path):
        return []
    frames: list[bytes] = []
    try:
        probe = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "default=noprint_wrappers=1:nokey=1", video_path],
            capture_output=True, text=True, timeout=5, shell=False,
        )
        dur = float(probe.stdout.strip()) if probe.returncode == 0 and probe.stdout.strip() else 0.0
        timestamps = (
            [dur * i / (num_frames + 1) for i in range(1, num_frames + 1)]
            if dur > 0.3
            else [0.5]
        )
        for ts in timestamps:
            proc = subprocess.run(
                [
                    "ffmpeg", "-ss", f"{ts:.2f}", "-noaccurate_seek", "-i", video_path,
                    "-frames:v", "1", "-vf", "scale='min(720,iw)':-2",
                    "-f", "image2pipe", "-vcodec", "mjpeg", "pipe:1",
                ],
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=5, shell=False,
            )
            if proc.returncode == 0 and proc.stdout:
                frames.append(proc.stdout)
    except Exception:
        return []
    return frames


def _build_v1_cloud_translation_prompt(
    chunk_segments: list[dict],
    *,
    speaker_gender: str = "unknown",
    script_mode: str = "default",
    glossary: dict[str, str] | None = None,
    prior_context: list[str] | None = None,
    video_duration: float | None = None,
    has_frames: bool = False,
) -> str:
    is_humorous = str(script_mode or "default").strip().lower() in {"humorous", "hai_huoc", "haihuoc", "comedy"}
    if is_humorous:
        style_line = (
            "Phong cách HÀI HƯỚC, TẤU HÀI DÍ DỎM văn minh cho video ngắn TikTok/Douyin (gần gũi, duyên dáng, "
            "tuyệt đối không thô tục, giữ chính xác 100% số liệu/thông số/hướng thao tác). "
        )
    else:
        style_line = (
            "Dịch từ tiếng Trung sang tiếng Việt chuẩn, tự nhiên, cuốn hút, văn phong đời thực cho video ngắn TikTok/Douyin. "
        )

    spk = str(speaker_gender or "unknown").strip().lower()
    speaker_rule = ""
    if spk in ("male", "female"):
        spk_vn = "nam giới" if spk == "male" else "nữ giới"
        speaker_rule = f"Người nói chính trong video là {spk_vn} (speaker='{spk}'); nếu câu gốc có đại từ nhân xưng thì giữ xưng hô nhất quán phù hợp {spk_vn}. "

    glossary_rule = ""
    if glossary:
        src_joined = "\n".join(str(s.get("text") or "") for s in chunk_segments)
        matched = {k: v for k, v in glossary.items() if k and k in src_joined}
        if matched:
            pairs = ", ".join(f"'{k}' -> '{v}'" for k, v in list(matched.items())[:60])
            glossary_rule = f"Từ điển thuật ngữ bắt buộc (Custom Glossary): {pairs}. "

    vision_rule = (
        "Kết hợp các khung hình đính kèm để nhận biết chính xác đồ vật, bối cảnh và thao tác trong video. "
        if has_frames
        else ""
    )

    input_items = []
    for idx, s in enumerate(chunk_segments):
        st = float(s["start"])
        et = float(s["end"])
        next_st = (
            float(chunk_segments[idx + 1]["start"]) - 0.04
            if idx + 1 < len(chunk_segments)
            else (float(video_duration) if video_duration else et + 0.5)
        )
        slot_s = max(0.3, min(next_st, et + 1.2) - st)
        max_words = max(2, int(round(max(0.45, slot_s - 0.05) * 3.2)))
        item: dict = {
            "id": int(s["id"]),
            "text": s["text"],
            "duration_seconds": round(et - st, 3),
            "max_words": max_words,
        }
        if spk in ("male", "female"):
            item["speaker"] = spk
        bb = s.get("best_block")
        ocr_txt = str(bb.get("text") or "").strip() if isinstance(bb, dict) else ""
        if ocr_txt and ocr_txt != str(s["text"]).strip():
            item["ocr_hint"] = ocr_txt[:120]
        input_items.append(item)

    ctx_block = ""
    if prior_context:
        ctx_block = (
            "Ngữ cảnh câu trước (chỉ để giữ mạch ý & xưng hô, KHÔNG dịch lại): "
            + json.dumps(list(prior_context)[-4:], ensure_ascii=False)
            + "\n"
        )

    return (
        "Bạn là chuyên gia dịch thuật và lồng tiếng video ngắn chuẩn Tool V1. "
        "Đọc TOÀN BỘ danh sách để nắm mạch hành động trước khi dịch từng cue. "
        + style_line
        + speaker_rule
        + vision_rule
        + glossary_rule
        + "Dịch thuật ngữ/nghĩa bóng theo ngữ cảnh và tên vật dụng thông dụng tiếng Việt (ví dụ '懒春秋' là thư thái nhàn hạ; "
        "bao bì/sưu tầm: '自封袋'=túi zip, '背卡'=bìa lót, '吧唧'=huy hiệu, '谷子'=đồ sưu tầm, '丝'=0,01 mm độ dày màng chứ không phải lụa; "
        "câu kêu gọi '关注我/点赞' Việt hóa tự nhiên ngắn gọn). "
        "Không được bỏ sót, không gộp hay tách câu, không trùng id, không giữ chữ Trung Quốc. "
        "Giữ đúng nghĩa, phủ định, điều kiện và mọi số lượng/đơn vị gốc; không quy đổi tiền, TUYỆT ĐỐI không tự thêm đơn vị tiền ('tệ', 'đồng', 'đô', 'yên') hay bậc giá ('nghìn', 'ngàn', 'triệu', 'tỷ') khi câu gốc không ghi trực tiếp (ví dụ '个位数' dịch là 'giá một chữ số', '两位数' dịch là 'giá hai chữ số', '白菜价' dịch là 'giá cực rẻ', KHÔNG dịch thành 'vài tệ' hay 'vài nghìn'). "
        "Không thêm tính chất, lời cảm thán hoặc quảng cáo mà nguồn không có; không bỏ tên riêng, tên chất liệu hay hướng thao tác để rút ngắn. "
        "Tên riêng Trung Quốc phải phiên âm Latin phù hợp, không thay bằng từ chung chung. "
        "Mỗi câu phải súc tích vừa khung đọc duration_seconds (ưu tiên <= max_words từ tiếng Việt). "
        "BẮT BUỘC giữ nguyên định dạng JSON list: [{\"id\": ..., \"text\": \"...\"}]:\n"
        + ctx_block
        + json.dumps(input_items, ensure_ascii=False)
    )


def _repair_monetary_cues_in_chunk(
    client,
    types_mod,
    model_name: str,
    chunk: list[dict],
    translated_data: list,
) -> list:
    """Sửa cục bộ các cue bị lỗi gác tiền tệ/bậc số mà không vứt bỏ cả lô 40 câu hợp lệ."""
    if not isinstance(translated_data, list):
        return translated_data
    by_id: dict[int, dict] = {}
    for item in translated_data:
        if isinstance(item, dict) and item.get("id") is not None and not isinstance(item.get("id"), bool):
            try:
                by_id[int(item["id"])] = item
            except (TypeError, ValueError):
                pass

    bad_payload = []
    for seg in chunk:
        sid = int(seg["id"])
        cand = by_id.get(sid)
        if cand is None:
            return translated_data
        try:
            validate_translation_items([seg], [cand])
        except PipelineStageError as exc:
            msg = str(exc)
            if "đơn vị tiền" in msg or "bậc giá tiền" in msg:
                bad_payload.append({
                    "id": sid,
                    "source": seg["text"],
                    "rejected_translation": cand.get("text", ""),
                    "reason": msg,
                })
            else:
                return translated_data

    if not bad_payload:
        return translated_data

    repair_prompt = (
        "Các câu dưới đây bị bộ kiểm duyệt từ chối vì bản dịch tự thêm đơn vị tiền tệ ('tệ', 'nhân dân tệ', 'đồng', 'đô', 'yên') "
        "hoặc bậc số ('nghìn', 'ngàn', 'triệu', 'tỷ') không xuất hiện trực tiếp trong chữ gốc tiếng Trung. "
        "Hãy dịch lại sát nghĩa chữ gốc sang tiếng Việt tự nhiên, TUYỆT ĐỐI KHÔNG dùng các từ 'tệ', 'nhân dân tệ', 'đồng', 'đô', 'yên', 'nghìn', 'ngàn', 'triệu', 'tỷ' "
        "trừ khi chữ gốc tiếng Trung có ghi rõ con số/đơn vị tương ứng (ví dụ: '个位数' -> 'giá một chữ số' hoặc 'một chữ số'; '两位数' -> 'hai chữ số'; '很便宜/白菜价' -> 'rất rẻ'). "
        "Chỉ trả về JSON list [{\"id\": ..., \"text\": \"...\"}]:\n"
        + json.dumps(bad_payload, ensure_ascii=False)
    )
    resp = client.models.generate_content(
        model=model_name,
        contents=repair_prompt,
        config=types_mod.GenerateContentConfig(response_mime_type="application/json", temperature=0.0),
    )
    repaired_items = json.loads(_strip_json_fences(getattr(resp, "text", "") or ""))
    if not isinstance(repaired_items, list):
        return translated_data
    rep_map = {}
    for r_item in repaired_items:
        if isinstance(r_item, dict) and r_item.get("id") is not None and isinstance(r_item.get("text"), str):
            try:
                rep_map[int(r_item["id"])] = r_item["text"].strip()
            except (TypeError, ValueError):
                pass
    merged_list = []
    for item in translated_data:
        if isinstance(item, dict) and item.get("id") is not None:
            try:
                iid = int(item["id"])
                if iid in rep_map and rep_map[iid]:
                    merged_list.append({**item, "text": rep_map[iid]})
                    continue
            except (TypeError, ValueError):
                pass
        merged_list.append(item)
    return merged_list


def _resolve_cloud_gemini_keys(explicit_key: str = "") -> list[str]:
    """Thu thập toàn bộ danh sách GEMINI_API_KEYS / GEMINI_API_KEY để xoay vòng chống 429 chuẩn V1."""
    def _split_keys(raw: str) -> list[str]:
        out: list[str] = []
        for part in re.split(r"[\s,;]+", str(raw or "")):
            cleaned = part.strip().strip('"').strip("'")
            if cleaned and cleaned not in out:
                out.append(cleaned)
        return out

    primary_list = _split_keys(explicit_key) or _split_keys(os.getenv("GEMINI_API_KEY", ""))
    if not primary_list:
        return []
    pool_list = _split_keys(os.getenv("GEMINI_API_KEYS", ""))
    keys = list(primary_list)
    if pool_list and any(k in pool_list for k in primary_list):
        for pk in pool_list:
            if pk not in keys:
                keys.append(pk)
    return keys


_SHORTEN_DEMOTED_MODELS: set[str] = set()
_TRANSIENT_GENAI_MARKERS = ("503", "429", "404", "timeout", "timed out", "deadline", "unavailable")


def _make_genai_client(
    genai_mod,
    types_mod,
    api_key: str,
    *,
    timeout_ms: int = 25000,
    max_sdk_attempts: int = 1,
):
    http_opts_cls = getattr(types_mod, "HttpOptions", None)
    if callable(http_opts_cls):
        retry_cls = getattr(types_mod, "HttpRetryOptions", None)
        http_kwargs = {"timeout": timeout_ms}
        if callable(retry_cls):
            try:
                http_kwargs["retry_options"] = retry_cls(attempts=max_sdk_attempts)
            except TypeError:
                pass
        try:
            http_opts = http_opts_cls(**http_kwargs)
        except TypeError:
            try:
                http_opts = http_opts_cls(timeout=timeout_ms)
            except TypeError:
                http_opts = None
        if http_opts is not None:
            try:
                return genai_mod.Client(api_key=api_key, http_options=http_opts)
            except TypeError:
                pass
    return genai_mod.Client(api_key=api_key)


def step4_translate_subtitles(
    segments: list[dict],
    gemini_key: str,
    vi_srt_path: str = "subtitles_vi.srt",
    *,
    speaker_gender: str = "unknown",
    script_mode: str = "default",
    glossary: dict[str, str] | None = None,
    video_path: str | None = None,
    video_duration: float | None = None,
) -> list[dict]:
    log_step(4, 8, "Dịch phụ đề sang tiếng Việt tự nhiên chuẩn V1 (Gemini AI)...")
    if not segments:
        return segments

    api_keys = _resolve_cloud_gemini_keys(gemini_key)
    if not api_keys:
        raise PipelineStageError(
            "TRANSLATION_FAILED",
            "Thiếu GEMINI_API_KEY nên không thể dịch phụ đề sang tiếng Việt.",
        )

    candidate_models = [
        "gemini-3-flash-preview",
        "gemini-3.1-flash-lite-preview",
        "gemini-flash-lite-latest",
    ]
    max_attempts_per_model = max(2, min(len(api_keys), 4))
    trans_map: dict[int, str] = {}
    last_error = "Không nhận được phản hồi dịch hợp lệ."
    print(f"  -> Bể khóa Gemini trên Cloud: {len(api_keys)} API key khả dụng (xoay vòng tự động).", flush=True)

    try:
        from google import genai
        from google.genai import types

        clients = [
            _make_genai_client(genai, types, k, timeout_ms=90000, max_sdk_attempts=1)
            for k in api_keys
        ]
        frame_parts = []
        part_cls = getattr(types, "Part", None)
        if video_path and part_cls is not None and callable(getattr(part_cls, "from_bytes", None)):
            for jpg in _extract_keyframes_jpeg_bytes(video_path, num_frames=3):
                try:
                    frame_parts.append(part_cls.from_bytes(data=jpg, mime_type="image/jpeg"))
                except Exception:
                    break

        chunk_size = 40
        rolling_context: list[str] = []
        for chunk_idx, offset in enumerate(range(0, len(segments), chunk_size)):
            chunk = segments[offset : offset + chunk_size]
            prompt = _build_v1_cloud_translation_prompt(
                chunk,
                speaker_gender=speaker_gender,
                script_mode=script_mode,
                glossary=glossary,
                prior_context=rolling_context or None,
                video_duration=video_duration,
                has_frames=bool(frame_parts),
            )
            contents_payload = ([prompt] + frame_parts) if frame_parts else prompt
            chunk_map: dict[int, str] | None = None
            for model_name in candidate_models:
                for attempt_idx in range(1, max_attempts_per_model + 1):
                    key_slot = (chunk_idx + attempt_idx - 1) % len(clients)
                    active_client = clients[key_slot]
                    try:
                        response = active_client.models.generate_content(
                            model=model_name,
                            contents=contents_payload,
                            config=types.GenerateContentConfig(response_mime_type="application/json"),
                        )
                        raw_json = _strip_json_fences(getattr(response, "text", "") or "")
                        if not raw_json:
                            raise PipelineStageError("TRANSLATION_FAILED", f"Model {model_name} trả về phản hồi rỗng.")
                        translated_data = json.loads(raw_json)
                        try:
                            chunk_map = validate_translation_items(chunk, translated_data)
                        except PipelineStageError as val_err:
                            if "đơn vị tiền" in str(val_err) or "bậc giá tiền" in str(val_err):
                                repaired_data = _repair_monetary_cues_in_chunk(
                                    active_client, types, model_name, chunk, translated_data
                                )
                                chunk_map = validate_translation_items(chunk, repaired_data)
                            else:
                                raise
                        print(
                            f"  -> Đã dịch & xác thực đủ {len(chunk)} câu thoại bằng {model_name} (key #{key_slot + 1}/{len(clients)}, lần {attempt_idx}).",
                            flush=True,
                        )
                        break
                    except Exception as m_err:
                        safe_m_err = _redact_secrets_local(str(m_err), extra_secrets=tuple(api_keys))
                        last_error = f"{model_name} (key #{key_slot + 1}, lần {attempt_idx}): {safe_m_err}"
                        print(f"  [WARN] Dịch thất bại với {last_error}", flush=True)
                        if attempt_idx < max_attempts_per_model:
                            time.sleep(0.5 * attempt_idx)
                if chunk_map is not None:
                    break

            if chunk_map is None:
                trans_map = {}
                break
            trans_map.update(chunk_map)
            rolling_context.extend(chunk_map[int(s["id"])] for s in chunk)
    except PipelineStageError:
        raise
    except Exception as exc:
        safe_exc = _redact_secrets_local(str(exc), extra_secrets=tuple(api_keys))
        raise PipelineStageError("TRANSLATION_FAILED", f"Lỗi khởi tạo Gemini Client: {safe_exc}") from exc

    if len(trans_map) != len(segments):
        raise PipelineStageError(
            "TRANSLATION_FAILED",
            f"Dịch phụ đề thất bại hoặc thiếu câu thoại sau khi thử lại: {last_error}",
        )

    for s in segments:
        s["translated_text"] = trans_map[int(s["id"])]

    # Ghi file phụ đề tiếng Việt (subtitles_vi.srt) phục vụ lồng phụ đề (Hardsub)
    try:
        with open(vi_srt_path, "w", encoding="utf-8") as f:
            for idx, seg in enumerate(segments, 1):
                start_ts = format_timestamp(seg["start"])
                end_ts = format_timestamp(seg["end"])
                text_vi = seg["translated_text"]
                f.write(f"{idx}\n{start_ts} --> {end_ts}\n{text_vi}\n\n")
        if not os.path.isfile(vi_srt_path) or os.path.getsize(vi_srt_path) <= 0:
            raise PipelineStageError("SUBTITLE_BUILD_FAILED", f"File phụ đề tiếng Việt {vi_srt_path} rỗng.")
        print(f"  -> Đã ghi file phụ đề tiếng Việt: {vi_srt_path}", flush=True)
    except PipelineStageError:
        raise
    except Exception as e:
        raise PipelineStageError("SUBTITLE_BUILD_FAILED", f"Không thể ghi file {vi_srt_path}: {e}") from e

    return segments


def validate_tts_audio_file(
    path: str,
    *,
    min_duration_s: float = 0.05,
    max_duration_s: float = 120.0,
) -> float:
    """Kiểm tra file âm thanh tồn tại, vượt ngưỡng tối thiểu, decode được và có duration hợp lệ."""
    if not path or not os.path.isfile(path):
        raise ValueError(f"File âm thanh TTS không tồn tại: {path}")
    file_size = os.path.getsize(path)
    if file_size <= 128:
        raise ValueError(f"File âm thanh TTS quá nhỏ ({file_size} bytes): {path}")

    dur_s: float | None = None
    probe_errors: list[str] = []
    try:
        proc = subprocess.run(
            [
                "ffprobe",
                "-v",
                "error",
                "-show_entries",
                "format=duration",
                "-of",
                "default=noprint_wrappers=1:nokey=1",
                path,
            ],
            capture_output=True,
            text=True,
            timeout=15,
            shell=False,
        )
        if proc.returncode == 0 and proc.stdout.strip():
            dur_s = float(proc.stdout.strip())
        elif proc.stderr.strip():
            probe_errors.append(proc.stderr.strip())
    except Exception as exc:
        probe_errors.append(str(exc))

    if dur_s is None:
        try:
            from pydub import AudioSegment

            seg_audio = AudioSegment.from_file(path)
            dur_s = len(seg_audio) / 1000.0
        except Exception as exc:
            probe_errors.append(f"pydub: {exc}")

    if dur_s is None or not math.isfinite(dur_s):
        raise ValueError(f"Không thể decode file âm thanh TTS {path}: {'; '.join(probe_errors)}")
    if dur_s < min_duration_s or dur_s > max_duration_s:
        raise ValueError(
            f"Thời lượng TTS {dur_s:.3f}s ngoài khoảng hợp lệ [{min_duration_s}, {max_duration_s}]: {path}"
        )
    return float(dur_s)


def generate_capcut_tts_single(
    text: str,
    voice: str,
    out_file: str,
    *,
    max_retries: int = 3,
    poll_timeout: float = 12.0,
) -> bool:
    """Gọi CapCut TTS SDK với retry có giới hạn và kiểm tra decode file audio trước khi chấp nhận."""
    tmp_out = f"{out_file}.part"
    attempts = max(1, int(max_retries))
    for attempt in range(1, attempts + 1):
        try:
            if os.path.exists(tmp_out):
                os.remove(tmp_out)
            from capcut_tts_api import CapCutClient

            client = CapCutClient()
            res = client.create_tts_task(texts=text, voice=voice)
            tasks = res.get("data", {}).get("tasks", [])
            if not tasks:
                raise RuntimeError("CapCut TTS không trả về task id.")
            task_id = tasks[0]["id"]
            token = tasks[0]["token"]

            deadline = time.monotonic() + max(1.0, float(poll_timeout))
            downloaded = False
            poll_round = 0
            while time.monotonic() < deadline:
                time.sleep(0.20 if poll_round == 0 else 0.30)
                poll_round += 1
                query_res = client.query_tts_task(task_id, token)
                q_tasks = query_res.get("data", {}).get("tasks", [])
                if not q_tasks:
                    break
                status = q_tasks[0].get("status")
                if status in ("success", "succeed"):
                    payload = json.loads(q_tasks[0].get("payload", "{}"))
                    speech_url = payload.get("audio_subtitles", [{}])[0].get("speech_url")
                    if speech_url:
                        r = client.session.get(speech_url, timeout=15)
                        r.raise_for_status()
                        if len(r.content) > 128:
                            with open(tmp_out, "wb") as f:
                                f.write(r.content)
                            validate_tts_audio_file(tmp_out)
                            os.replace(tmp_out, out_file)
                            downloaded = True
                            break
                elif status == "failed":
                    raise RuntimeError("CapCut TTS task trả về status=failed.")
            if downloaded:
                return True
            raise RuntimeError("Hết thời gian chờ hoặc âm thanh CapCut TTS không hợp lệ.")
        except Exception as exc:
            print(f"    [CapCut TTS Warn attempt {attempt}/{attempts}] {exc}", flush=True)
            if os.path.exists(tmp_out):
                try:
                    os.remove(tmp_out)
                except OSError:
                    pass
            if attempt < attempts:
                time.sleep(min(2.0, 0.5 * attempt))
    return False


async def generate_single_tts(text: str, voice: str, out_file: str):
    import edge_tts

    tmp_out = f"{out_file}.part"
    if os.path.exists(tmp_out):
        os.remove(tmp_out)
    communicate = edge_tts.Communicate(text, voice)
    await communicate.save(tmp_out)
    validate_tts_audio_file(tmp_out)
    os.replace(tmp_out, out_file)


TTS_NATURAL_MAX_SPEED = 1.50
TTS_EMERGENCY_MAX_SPEED = 1.75
TTS_MAX_PRE_BORROW_S = 0.22


def _trim_tts_silence_in_process(
    audio_path: str,
    *,
    silence_thresh_db: float = -45.0,
    pad_ms: int = 35,
) -> str:
    """Cắt khoảng lặng thừa đầu/cuối của CapCut/Edge MP3 (< -45 dBFS) sang PCM WAV chuẩn Tool V1 Local."""
    if not audio_path or not os.path.isfile(audio_path):
        return audio_path
    try:
        if os.path.getsize(audio_path) < 512:
            return audio_path
        import wave
        from pydub import AudioSegment
        from pydub.silence import detect_leading_silence

        audio = AudioSegment.from_file(audio_path)
        total_ms = len(audio)
        if total_ms < 160:
            return audio_path
        lead_ms = int(detect_leading_silence(audio, silence_threshold=silence_thresh_db, chunk_size=5))
        trail_ms = int(detect_leading_silence(audio.reverse(), silence_threshold=silence_thresh_db, chunk_size=5))
        start_trim = max(0, lead_ms - pad_ms)
        end_trim = min(total_ms, total_ms - max(0, trail_ms - pad_ms))
        if (start_trim < 15 and (total_ms - end_trim) < 15) or (end_trim - start_trim) < 80:
            return audio_path
        trimmed = audio[start_trim:end_trim]
        out_wav = f"{audio_path}.trim.wav"
        with wave.open(out_wav, "wb") as wf:
            wf.setnchannels(trimmed.channels)
            wf.setsampwidth(trimmed.sample_width)
            wf.setframerate(trimmed.frame_rate)
            wf.writeframes(trimmed.raw_data)
        return out_wav if os.path.isfile(out_wav) and os.path.getsize(out_wav) > 128 else audio_path
    except Exception:
        return audio_path


def _shorten_translation_for_timing(
    segment: dict,
    window_s: float,
    gemini_key: str,
    *,
    measured_duration_s: float | None = None,
    key_offset: int = 0,
) -> str:
    """Rephrase from source for a bounded TTS retry; never crop speech to fit."""
    api_keys = _resolve_cloud_gemini_keys(gemini_key)
    if not api_keys:
        raise PipelineStageError("TTS_GENERATION_FAILED", "TTS vượt khung; thiếu khóa để dịch lại súc tích.")
    from google import genai
    from google.genai import types
    clients = [
        _make_genai_client(genai, types, k, timeout_ms=20000, max_sdk_attempts=1)
        for k in api_keys
    ]
    prev_text = str(segment.get("translated_text") or "").strip()
    prev_words = len(prev_text.split())
    if measured_duration_s and measured_duration_s > 0.1 and prev_words > 1:
        target_words = max(1, min(prev_words - 1, int(math.floor(prev_words * (window_s * 1.35 / measured_duration_s)))))
    else:
        target_words = max(1, int(window_s * 2.8))
        if prev_words > 1:
            target_words = min(target_words, prev_words - 1)

    prompt = (
        "Dịch lại câu nguồn sang tiếng Việt CỰC KỲ NGẮN GỌN để khớp thời lượng lồng tiếng. "
        f"BẮT BUỘC rút gọn còn TỐI ĐA {target_words} từ đơn (âm tiết) tiếng Việt, ngắn hơn bản cũ ({prev_words} từ). "
        "Lược bỏ toàn bộ hư từ/trợ từ/đại từ thừa ('các bạn', 'chúng ta', 'mọi người', 'nhé', 'nha', 'nè', 'thì', 'là', 'mà', 'cực kỳ' -> 'rất'), "
        "dùng từ ghép cô đọng nhất nhưng giữ ĐẦY ĐỦ ý cốt lõi, phủ định, con số, tên riêng/chất liệu và hướng thao tác. "
        "Không tự đổi tiền, không thêm 'tệ/đồng/nghìn/triệu' nếu chữ gốc không ghi rõ ('个位数' -> 'một chữ số'), không thêm cảm thán. "
        "Trả JSON list [{\"id\": ..., \"text\": \"...\"}].\n"
        + json.dumps({"id": segment["id"], "source": segment["text"],
                      "previous_translation": prev_text, "target_seconds": round(window_s, 3),
                      "target_vietnamese_words": target_words}, ensure_ascii=False)
    )
    last_error = "No valid concise translation"
    call_idx = max(0, int(key_offset))
    base_models = ("gemini-3-flash-preview", "gemini-3.1-flash-lite-preview", "gemini-flash-lite-latest")
    ordered_models = [m for m in base_models if m not in _SHORTEN_DEMOTED_MODELS] + [
        m for m in base_models if m in _SHORTEN_DEMOTED_MODELS
    ]
    for model in ordered_models:
        transient_failures = 0
        for attempt in range(max(2, len(clients))):
            active_client = clients[call_idx % len(clients)]
            call_idx += 1
            try:
                response = active_client.models.generate_content(model=model, contents=prompt,
                    config=types.GenerateContentConfig(response_mime_type="application/json", temperature=0.0))
                data = json.loads(_strip_json_fences(getattr(response, "text", "") or ""))
                return validate_translation_items([segment], data)[int(segment["id"])]
            except Exception as exc:
                last_error = _redact_secrets_local(str(exc), extra_secrets=tuple(api_keys))
                lowered_err = last_error.lower()
                if any(marker in lowered_err for marker in _TRANSIENT_GENAI_MARKERS):
                    transient_failures += 1
                    if transient_failures >= 2:
                        _SHORTEN_DEMOTED_MODELS.add(model)
                else:
                    transient_failures = 0
                if attempt == 0:
                    time.sleep(1.0)
    raise PipelineStageError("TTS_GENERATION_FAILED", f"Dịch lại súc tích thất bại sau retry giới hạn: {last_error}")


def step5_generate_tts(
    segments: list[dict],
    voice: str,
    tts_dir: str,
    *,
    allow_no_speech: bool = False,
    allow_edge_tts_fallback: bool = False,
    video_duration: float | None = None,
    gemini_key: str = "",
) -> dict:
    if not segments:
        if allow_no_speech:
            print("  -> Video ở chế độ no_speech_mode (cố ý không lời).", flush=True)
            return {
                "tts_engine": "none",
                "tts_voice": voice,
                "verified_count": 0,
                "adjusted_count": 0,
                "fallback_used": False,
            }
        raise PipelineStageError(
            "NO_SPEECH_DETECTED",
            "Không phát hiện câu thoại nào trong video; chặn xuất bản lồng tiếng rỗng.",
        )

    is_edge_explicit = voice.lower().startswith("vi-vn-")
    is_capcut = (voice.startswith("BV") or "capcut" in voice.lower()) and not is_edge_explicit
    capcut_voice = voice.replace("capcut-", "") if "capcut-" in voice else voice
    if is_capcut and not capcut_voice.startswith("BV"):
        capcut_voice = "BV562_streaming"

    edge_fallback = "vi-VN-NamMinhNeural" if ("075" in voice or "male" in voice.lower()) else "vi-VN-HoaiMyNeural"
    edge_voice_target = voice if is_edge_explicit else edge_fallback
    engine_name = f"CapCut TTS ({capcut_voice})" if is_capcut else f"Edge TTS ({edge_voice_target})"

    log_step(5, 8, f"Tổng hợp giọng đọc lồng tiếng ({engine_name})...")
    os.makedirs(tts_dir, exist_ok=True)
    fallback_used = False
    total_segs = len(segments)
    progress_stride = max(1, total_segs // 6)

    for s in segments:
        if not str(s.get("translated_text") or "").strip():
            raise PipelineStageError(
                "TRANSLATION_FAILED",
                f"Câu #{s['id']} thiếu bản dịch tiếng Việt hợp lệ trước khi tạo TTS.",
            )

    try:
        tts_concurrency = min(6, max(1, int(os.getenv("CLOUD_TTS_CONCURRENCY", "5") or 5)))
    except ValueError:
        tts_concurrency = 5

    async def _run_all():
        nonlocal fallback_used
        sem = asyncio.Semaphore(tts_concurrency)
        done_count = 0

        async def _synth_one(s: dict) -> None:
            nonlocal fallback_used, done_count
            t_text = str(s.get("translated_text") or "").strip()
            out_path = os.path.join(tts_dir, f"seg_{int(s['id']):04d}.mp3")
            s["tts_path"] = out_path

            async with sem:
                success = False
                if is_capcut:
                    success = await asyncio.to_thread(generate_capcut_tts_single, t_text, capcut_voice, out_path)
                    if success:
                        s["tts_engine_used"] = "capcut_tts"
                    elif allow_edge_tts_fallback:
                        for edge_try in range(1, 3):
                            try:
                                await generate_single_tts(t_text, edge_fallback, out_path)
                                success = True
                                fallback_used = True
                                s["tts_engine_used"] = "edge_tts_fallback"
                                break
                            except Exception as e:
                                print(f"  [WARN] Edge fallback lần {edge_try} đoạn {s['id']}: {e}", flush=True)
                else:
                    for edge_try in range(1, 3):
                        try:
                            await generate_single_tts(t_text, edge_voice_target, out_path)
                            success = True
                            s["tts_engine_used"] = "edge_tts"
                            break
                        except Exception as e:
                            print(f"  [WARN] Edge TTS lần {edge_try} đoạn {s['id']}: {e}", flush=True)

                if not success:
                    raise PipelineStageError(
                        "TTS_GENERATION_FAILED",
                        f"Tổng hợp giọng đọc TTS thất bại cho câu #{s['id']} (engine={engine_name}).",
                    )
                done_count += 1
                if done_count == total_segs or done_count % progress_stride == 0:
                    pct = min(80, 71 + int(9 * done_count / max(1, total_segs)))
                    _write_pipeline_progress(pct, 5, 8, f"Tổng hợp giọng đọc {engine_name} ({done_count}/{total_segs} câu)...")

        await asyncio.gather(*(_synth_one(seg_item) for seg_item in segments))

    asyncio.run(_run_all())
    _write_pipeline_progress(81, 5, 8, "Căn chỉnh thời lượng giọng đọc chống đè lời chuẩn Tool V1...")

    # Nén tốc độ FFmpeg atempo + cắt lặng thừa đầu/cuối đồng bộ 100% Tool V1 Local
    try:
        from concurrent.futures import ThreadPoolExecutor
        import threading
        from v1_timing_solver import build_safe_atempo_filter

        fit_lock = threading.Lock()
        fit_done = [0]

        def _prepare_cue_timing(idx_and_seg: tuple[int, dict]) -> dict:
            idx, s = idx_and_seg
            tts_p = _trim_tts_silence_in_process(str(s.get("tts_path") or ""))
            s["tts_path"] = tts_p
            raw_dur = validate_tts_audio_file(str(tts_p or ""))
            next_boundary = (
                float(segments[idx + 1]["start"]) - 0.04
                if idx + 1 < len(segments)
                else (float(video_duration) - 0.02 if video_duration else float(s["end"]) + 1.5)
            )
            boundary = min(next_boundary, max(float(s["end"]) + 2.5, float(s["start"]) + 3.5))
            src_start = float(s["start"])
            base_win_dur = boundary - src_start
            if base_win_dur <= 0.05:
                raise PipelineStageError("TTS_GENERATION_FAILED", f"Cue #{s['id']} không có khung đọc hợp lệ.")

            history = [{"text": s["translated_text"], "raw_duration": raw_dur}]
            base_fit_window = max(0.05, base_win_dur - 0.01)

            best_text = str(s["translated_text"])
            best_path = str(tts_p)
            best_dur = float(raw_dur)

            for retry in range(3):
                if best_dur / base_fit_window <= TTS_NATURAL_MAX_SPEED:
                    break
                effective_win = base_win_dur * (0.78 ** retry)
                eff_key = gemini_key or os.getenv("GEMINI_API_KEY", "")
                try:
                    replacement = _shorten_translation_for_timing(
                        s,
                        effective_win,
                        eff_key,
                        measured_duration_s=best_dur,
                        key_offset=idx,
                    )
                except TypeError:
                    replacement = _shorten_translation_for_timing(
                        s,
                        effective_win,
                        eff_key,
                    )
                if not replacement or replacement == s["translated_text"]:
                    break
                s["translated_text"] = replacement
                cand_mp3 = os.path.join(tts_dir, f"seg_{int(s['id']):04d}_r{retry + 1}.mp3")
                if is_capcut:
                    if not generate_capcut_tts_single(replacement, capcut_voice, str(cand_mp3)):
                        raise PipelineStageError("TTS_GENERATION_FAILED", f"TTS dịch lại thất bại ở cue #{s['id']}.")
                else:
                    asyncio.run(generate_single_tts(replacement, edge_voice_target, str(cand_mp3)))
                cand_path = _trim_tts_silence_in_process(str(cand_mp3))
                cand_dur = validate_tts_audio_file(str(cand_path))
                history.append({"text": replacement, "raw_duration": cand_dur})
                if cand_dur < best_dur:
                    best_text = replacement
                    best_path = cand_path
                    best_dur = cand_dur

            s["translated_text"] = best_text
            s["tts_path"] = best_path
            with fit_lock:
                fit_done[0] += 1
                cur_n = fit_done[0]
                if cur_n == 1 or cur_n == total_segs or cur_n % progress_stride == 0:
                    _write_pipeline_progress(
                        81,
                        5,
                        8,
                        f"Căn chỉnh thời lượng giọng đọc chống đè lời ({cur_n}/{total_segs} câu)...",
                    )
            return {
                "tts_p": best_path,
                "raw_dur": best_dur,
                "boundary": boundary,
                "src_start": src_start,
                "base_fit_window": base_fit_window,
                "history": history,
            }

        fit_workers = min(4, max(1, total_segs))
        with ThreadPoolExecutor(max_workers=fit_workers) as pool:
            prepared_cues = list(pool.map(_prepare_cue_timing, enumerate(segments)))

        adjusted = 0
        cue_audits = []
        prev_audio_end = 0.0
        for idx, (s, prep) in enumerate(zip(segments, prepared_cues)):
            tts_p = prep["tts_p"]
            raw_dur = prep["raw_dur"]
            boundary = prep["boundary"]
            src_start = prep["src_start"]
            base_fit_window = prep["base_fit_window"]
            history = prep["history"]

            min_allowed_start = (prev_audio_end + 0.04) if idx > 0 else 0.0
            avail_pre_gap = min(TTS_MAX_PRE_BORROW_S, max(0.0, src_start - min_allowed_start))

            # Mượn khoảng lặng trống ngay trước câu (tối đa 220ms, chuẩn voice_cloning.py Local) nếu cần giảm tốc độ nói
            pre_shift = 0.0
            if avail_pre_gap > 0.01 and raw_dur > base_fit_window:
                needed_extra = max(0.0, (raw_dur / 1.35) - base_fit_window)
                pre_shift = min(avail_pre_gap, max(raw_dur - base_fit_window, needed_extra))
            tts_start = max(0.0, src_start - pre_shift)
            win_dur = boundary - tts_start
            fit_window = max(0.05, win_dur - 0.01)

            speed = max(1.0, raw_dur / fit_window)
            if speed > TTS_EMERGENCY_MAX_SPEED + 1e-6:
                raise PipelineStageError(
                    "TTS_GENERATION_FAILED",
                    f"Cue #{s['id']} cần {speed:.2f}x sau dịch lại; chặn cắt/đè lời (giới hạn {TTS_EMERGENCY_MAX_SPEED:.2f}x).",
                )
            original_duration = raw_dur
            if speed > 1.01:
                af = build_safe_atempo_filter(speed)
                if af and af != "anull":
                    tmp_fit = f"{tts_p}.fit.wav"
                    fit_cmd = ["ffmpeg", "-y", "-i", tts_p, "-filter:a", af, "-c:a", "pcm_s16le", tmp_fit]
                    fit_res = subprocess.run(fit_cmd, capture_output=True, text=True)
                    if fit_res.returncode != 0:
                        if os.path.exists(tmp_fit):
                            os.remove(tmp_fit)
                        raise PipelineStageError(
                            "TTS_GENERATION_FAILED",
                            f"FFmpeg atempo thất bại cho câu #{s['id']}: {fit_res.stderr[-200:]}",
                        )
                    raw_dur = validate_tts_audio_file(tmp_fit)
                    tts_p = tmp_fit
                    s["tts_path"] = tts_p
                    adjusted += 1
            s["tts_start"] = round(tts_start, 4)
            s["tts_duration"] = raw_dur
            s["tts_speed"] = speed
            prev_audio_end = tts_start + raw_dur
            if tts_start + raw_dur > boundary + 0.035:
                raise PipelineStageError("TTS_GENERATION_FAILED", f"TTS cue #{s['id']} vẫn vượt biên sau atempo.")
            cue_audits.append({"id": s["id"], "source_text": s["text"], "text": s["translated_text"],
                "start": s["start"], "tts_start": s["tts_start"], "source_end": s["end"], "boundary": boundary,
                "raw_duration": original_duration, "duration": raw_dur, "speed": speed,
                "audio_sha256": _file_sha256(tts_p), "history": history})
        print(f"  -> Đã xác thực {len(segments)}/{len(segments)} câu TTS; cân chỉnh atempo {adjusted} câu.", flush=True)
    except PipelineStageError:
        raise
    except Exception as exc:
        raise PipelineStageError("TTS_GENERATION_FAILED", f"Lỗi xử lý thời lượng/timing TTS: {exc}") from exc

    return {
        "tts_engine": "capcut_tts" if is_capcut else "edge_tts",
        "tts_voice": capcut_voice if is_capcut else edge_voice_target,
        "verified_count": len(segments),
        "adjusted_count": adjusted,
        "fallback_used": fallback_used,
        "timing_passed": True,
        "cues": cue_audits,
    }


def step6_mix_audio_ducking_ebu_r128(
    bgm_wav: str,
    segments: list[dict],
    output_final_wav: str,
    *,
    allow_no_speech: bool = False,
    audio_cfg: dict | None = None,
):
    """
    Hòa âm Dynamic Ducking chuẩn EBU R128 (đồng bộ cài đặt Audio của Tool V1 Local):
    - Chặn xuất bản chỉ có nhạc nền khi video có thoại nhưng thiếu TTS.
    - Nhạc nền tự động nén xuống theo chế độ ducking_mode (soft / medium / off).
    - Chuẩn hóa âm lượng EBU R128 (-15.0 LUFS, True Peak -1.0 dBTP).
    """
    log_step(6, 8, "Hòa âm Dynamic Ducking & Chuẩn hóa EBU R128 (Chuẩn Tool V1)...")

    if not segments:
        if allow_no_speech:
            print("  -> Chế độ no_speech_mode: sao chép BGM đã kiểm tra.", flush=True)
            shutil.copy2(bgm_wav, output_final_wav)
            validate_tts_audio_file(output_final_wav, max_duration_s=7200.0)
            return
        raise PipelineStageError(
            "NO_SPEECH_DETECTED",
            "Không có câu thoại để hòa âm; chặn xuất bản chỉ có nhạc nền.",
        )

    dubbing_audio_files = []
    missing_cues: list[int] = []
    for s in segments:
        tts_file = s.get("tts_path")
        try:
            tts_dur = validate_tts_audio_file(str(tts_file or ""))
        except ValueError:
            missing_cues.append(int(s["id"]))
            continue
        start_val = float(s.get("tts_start", s["start"]))
        dubbing_audio_files.append({
            "path": str(tts_file),
            "start": start_val,
            "end": start_val + max(0.05, float(s.get("tts_duration") or tts_dur)),
            "index": int(s["id"]),
        })

    if missing_cues or len(dubbing_audio_files) != len(segments):
        raise PipelineStageError(
            "TTS_GENERATION_FAILED",
            f"Thiếu hoặc hỏng file TTS cho {len(missing_cues)}/{len(segments)} câu thoại ({missing_cues[:10]}).",
        )

    cfg = audio_cfg or {}
    bgm_gain_db = float(cfg.get("bgm_volume_db", -2.0))
    voice_gain_db = float(cfg.get("dubbing_volume_db", 1.0))
    duck_mode = str(cfg.get("ducking_mode") or "soft").strip().lower()
    if duck_mode == "off":
        duck_threshold, duck_ratio, duck_attack_ms, duck_release_ms = 1.0, 1.0, 20.0, 250.0
    elif duck_mode == "medium":
        duck_threshold, duck_ratio, duck_attack_ms, duck_release_ms = 0.025, 8.0, 20.0, 300.0
    else:
        # "soft" (mặc định Tool V1 Local): hạ nhạc nền êm dịu, tự nhiên khi có lời đọc
        duck_threshold, duck_ratio, duck_attack_ms, duck_release_ms = 0.03, 4.0, 25.0, 350.0

    # Thử hòa âm bằng module mixer chuẩn Tool V1 (FFmpeg native filter graph)
    mixed_ok = False
    try:
        from mixer import FFmpegMixSettings, mix_audio_ffmpeg

        settings = FFmpegMixSettings(
            background_gain_db=bgm_gain_db,
            voice_gain_db=voice_gain_db,
            duck_threshold=duck_threshold,
            duck_ratio=duck_ratio,
            duck_attack_ms=duck_attack_ms,
            duck_release_ms=duck_release_ms,
            target_lufs=-15.0,
            true_peak_dbtp=-1.0,
            loudness_range=11.0,
            output_sample_rate=48000,
        )

        print(
            f"  -> Chạy FFmpeg Sidechain Ducking ({duck_mode}, BGM={bgm_gain_db}dB, Voice={voice_gain_db}dB) "
            f"với {len(dubbing_audio_files)} câu thoại...",
            flush=True,
        )
        mix_res = mix_audio_ffmpeg(
            background_audio=bgm_wav,
            dubbing_audio_files=dubbing_audio_files,
            output_path=output_final_wav,
            settings=settings,
            ffmpeg_binary="ffmpeg",
            timeout_seconds=600.0,
        )
        print(f"  -> Hòa âm EBU R128 thành công: {mix_res.output_path} (số đoạn: {mix_res.dub_count})", flush=True)
        mixed_ok = True
    except Exception as exc:
        print(f"  [WARN] Lỗi FFmpeg mixer ({exc}). Tự động fallback sang Pydub overlay an toàn...", flush=True)

    if not mixed_ok:
        try:
            from pydub import AudioSegment

            bgm = AudioSegment.from_file(bgm_wav) - 6
            speech_track = AudioSegment.silent(duration=len(bgm))
            for item in dubbing_audio_files:
                seg_audio = AudioSegment.from_file(item["path"])
                pos_ms = int(item["start"] * 1000)
                speech_track = speech_track.overlay(seg_audio, position=pos_ms)

            mixed = bgm.overlay(speech_track)
            mixed.export(output_final_wav, format="wav")
            print("  -> Hòa âm Fallback Pydub hoàn tất.", flush=True)
        except Exception as exc:
            raise PipelineStageError("RENDER_FAILED", f"Hòa âm thất bại hoàn toàn: {exc}") from exc

    try:
        validate_tts_audio_file(output_final_wav, max_duration_s=7200.0)
    except ValueError as exc:
        raise PipelineStageError("RENDER_FAILED", f"File hòa âm đầu ra không hợp lệ: {exc}") from exc


def build_ass_subtitles_v1(
    segments: list[dict],
    ass_path: str,
    video_w: int = 1080,
    video_h: int = 1920,
    main_y_pct: float = 0.78,
    video_duration: float | None = None,
    *,
    cover_style: str = "solid_box",
    blur_settings: dict | None = None,
    font_name: str = "Arial",
    font_color: str = "&H00000000",
    font_weight: int = 2,
) -> bool:
    from subtitle_timing import SubtitleTimingError, generate_cloud_ass
    try:
        audit = generate_cloud_ass(
            segments,
            ass_path,
            video_w=video_w,
            video_h=video_h,
            main_y_pct=main_y_pct,
            video_duration=video_duration,
            cover_style=cover_style,
            blur_settings=blur_settings,
            font_name=font_name,
            font_color=font_color,
            font_weight=font_weight,
        )
        Path(ass_path).with_name("subtitle_timing.json").write_text(
            json.dumps(audit, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"  -> ASS source text/mask coverage verified ({audit.get('cover_style', cover_style)}): {ass_path}", flush=True)
        return True
    except (SubtitleTimingError, ValueError) as exc:
        raise PipelineStageError("SUBTITLE_BUILD_FAILED", f"ASS timing/coverage failed: {exc}") from exc


def _probe_main_video_cloud(input_video: str, fallback_w: int = 1080, fallback_h: int = 1920) -> dict:
    """Chọn đúng stream video chính (loại bỏ ảnh bìa attached_pic & xử lý xoay 90/270 độ như v1_media_streams.py)."""
    try:
        proc = subprocess.run(
            ["ffprobe", "-v", "error", "-show_streams", "-show_format", "-of", "json", str(input_video)],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=15, shell=False,
        )
        if proc.returncode == 0 and proc.stdout.strip():
            data = json.loads(proc.stdout)
            streams = data.get("streams") or []
            candidates = [
                s for s in streams
                if s.get("codec_type") == "video"
                and not int((s.get("disposition") or {}).get("attached_pic", 0) or 0)
                and int(s.get("width") or 0) > 0
                and int(s.get("height") or 0) > 0
            ]
            if candidates:
                chosen = min(
                    candidates,
                    key=lambda item: (
                        not bool(int((item.get("disposition") or {}).get("default", 0) or 0)),
                        int(item.get("index", 0)),
                    ),
                )
                width, height = int(chosen["width"]), int(chosen["height"])
                rotation = 0.0
                try:
                    rotation = float((chosen.get("tags") or {}).get("rotate", 0) or 0)
                except (TypeError, ValueError):
                    rotation = 0.0
                for side in chosen.get("side_data_list") or []:
                    if isinstance(side, dict) and "rotation" in side:
                        try:
                            rotation = float(side["rotation"])
                        except (TypeError, ValueError):
                            pass
                if round(rotation) % 180 == 90:
                    width, height = height, width
                dur = 0.0
                for raw_d in (chosen.get("duration"), (data.get("format") or {}).get("duration")):
                    try:
                        if raw_d is not None and float(raw_d) > 0:
                            dur = float(raw_d)
                            break
                    except (TypeError, ValueError):
                        continue
                idx = int(chosen.get("index", 0))
                return {
                    "stream_index": idx,
                    "input_map": f"0:{idx}",
                    "width": width,
                    "height": height,
                    "duration": dur,
                }
    except Exception:
        pass
    return {
        "stream_index": 0,
        "input_map": "0:v:0",
        "width": max(2, int(fallback_w or 1080)),
        "height": max(2, int(fallback_h or 1920)),
        "duration": 0.0,
    }


def _build_delogo_filters_cloud(dw: int, dh: int, delogo_mode: str) -> list[str]:
    mode = str(delogo_mode or "off").strip().lower()
    if mode != "corners" or dw <= 32 or dh <= 32:
        return []
    br_w, br_h = max(int(dw * 0.19), 10), max(int(dh * 0.065), 10)
    br_x, br_y = dw - br_w - int(dw * 0.01), dh - br_h - int(dh * 0.01)
    bl_w, bl_h = max(int(dw * 0.38), 10), max(int(dh * 0.065), 10)
    bl_x, bl_y = int(dw * 0.01), dh - bl_h - int(dh * 0.01)
    tl_w, tl_h = max(int(dw * 0.20), 10), max(int(dh * 0.055), 10)
    tl_x, tl_y = int(dw * 0.01), int(dh * 0.01)
    tr_w, tr_h = max(int(dw * 0.32), 10), max(int(dh * 0.055), 10)
    tr_x, tr_y = dw - tr_w - int(dw * 0.01), int(dh * 0.01)
    return [
        f"delogo=x={br_x}:y={br_y}:w={br_w}:h={br_h}",
        f"delogo=x={bl_x}:y={bl_y}:w={bl_w}:h={bl_h}",
        f"delogo=x={tl_x}:y={tl_y}:w={tl_w}:h={tl_h}",
        f"delogo=x={tr_x}:y={tr_y}:w={tr_w}:h={tr_h}",
    ]


def _build_v1_video_filter_graph(
    orig_w: int,
    orig_h: int,
    *,
    ass_path: str = "",
    vi_srt_path: str = "",
    main_y_pct: float = 0.78,
    canvas_cfg: dict | None = None,
) -> tuple[str, int, int, str]:
    """
    Xây dựng đồ thị bộ lọc hình ảnh chuẩn 100% như backend/video_utils.py (process_video):
    - Hỗ trợ localized_blur (làm mờ cục bộ đúng hộp chữ nguồn + chữ dịch từ ToolV1BlurData),
      frosted_blur, solid_box và adaptive_box.
    - Chuyển video không phải 9:16 sang khung dọc 9:16 (1080x1920) với nền mờ boxblur=8:3,
      burn phụ đề trên foreground [v2] ở độ phân giải gốc để tọa độ OCR khớp 100%,
      và ép format=yuv420p trước split tránh lỗi lệch kênh màu UV.
    """
    cfg = dict(DEFAULT_V1_RUNTIME_PROFILE["canvas"])
    if isinstance(canvas_cfg, dict):
        cfg.update(canvas_cfg)

    aspect_target = str(cfg.get("aspect_ratio") or "9:16").strip()
    cover_style = str(cfg.get("sub_cover_style") or "solid_box").strip().lower()
    if cover_style not in {"solid_box", "adaptive_box", "frosted_blur", "localized_blur"}:
        cover_style = "solid_box"
    delogo_mode = str(cfg.get("delogo_mode") or "off").strip().lower()

    has_ass = bool(ass_path and os.path.isfile(ass_path) and os.path.getsize(ass_path) > 80)
    has_srt = bool(vi_srt_path and os.path.isfile(vi_srt_path) and os.path.getsize(vi_srt_path) > 10)

    base_sub_filter = ""
    blur_meta = None
    if has_ass:
        escaped_ass = str(ass_path).replace("\\", "/").replace(":", "\\:")
        fonts_dir_cand = Path(ass_path).resolve().parent / "fonts"
        if not fonts_dir_cand.is_dir():
            fonts_dir_cand = Path("fonts").resolve()
        has_bundled_fonts = fonts_dir_cand.is_dir() and any(
            p.is_file() and p.suffix.lower() in {".ttf", ".otf", ".ttc"}
            for p in fonts_dir_cand.iterdir()
        )
        if has_bundled_fonts:
            escaped_fonts = str(fonts_dir_cand).replace("\\", "/").replace(":", "\\:")
            base_sub_filter = f"subtitles='{escaped_ass}':fontsdir='{escaped_fonts}'"
        else:
            base_sub_filter = f"subtitles='{escaped_ass}'"
        try:
            try:
                from subtitle_blur import read_ass_blur_metadata
            except ImportError:
                from backend.subtitle_blur import read_ass_blur_metadata
            blur_meta = read_ass_blur_metadata(Path(ass_path).read_text(encoding="utf-8-sig"))
        except Exception as exc:
            if cover_style == "localized_blur":
                raise PipelineStageError("RENDER_FAILED", f"Không đọc được metadata localized_blur từ ASS: {exc}") from exc
        if blur_meta is not None:
            cover_style = "localized_blur"
    elif has_srt:
        escaped_srt = str(vi_srt_path).replace("\\", "/").replace(":", "\\:")
        base_sub_filter = (
            f"subtitles='{escaped_srt}':force_style='FontSize=15,PrimaryColour=&H00FFFFFF,"
            "OutlineColour=&H00000000,BorderStyle=1,Outline=2,Shadow=1,MarginV=60'"
        )
        if cover_style == "localized_blur":
            cover_style = "solid_box"

    source_blur_boxes = []
    source_blur_settings = None
    if has_ass and cover_style == "localized_blur":
        if blur_meta is None:
            raise PipelineStageError(
                "RENDER_FAILED",
                "Chế độ localized_blur yêu cầu ToolV1BlurData đã xác minh trong file ASS.",
            )
        try:
            from subtitle_blur import build_localized_blur_filter, combine_blur_boxes, resolve_blur_settings
        except ImportError:
            from backend.subtitle_blur import build_localized_blur_filter, combine_blur_boxes, resolve_blur_settings
        source_blur_boxes = combine_blur_boxes(blur_meta["boxes"], blur_meta.get("text_boxes", []))
        source_blur_settings = resolve_blur_settings(cfg or blur_meta.get("settings"))

    def _sub_stage(fw: int, fh: int) -> str:
        if not base_sub_filter:
            return ""
        if cover_style == "localized_blur" and has_ass:
            blur_part = build_localized_blur_filter(source_blur_boxes, fw, fh, source_blur_settings)
            return (blur_part + "," if blur_part else "") + base_sub_filter
        if cover_style == "frosted_blur" and 0.15 <= float(main_y_pct or 0.78) <= 0.96:
            band_y = max(0, int(fh * max(0.0, float(main_y_pct) - 0.04)) // 2 * 2)
            band_h = max(24, min(fh - band_y, int(fh * 0.14)) // 2 * 2)
            return (
                f"split[sub_main][sub_band];"
                f"[sub_band]crop={fw}:{band_h}:0:{band_y},boxblur=16:3[sub_blurred];"
                f"[sub_main][sub_blurred]overlay=0:{band_y},{base_sub_filter}"
            )
        return base_sub_filter

    w, h = max(2, int(orig_w)), max(2, int(orig_h))
    is_already_9_16 = abs((w / h) - (9.0 / 16.0)) <= 0.035
    convert_to_9_16 = (aspect_target == "9:16") and not is_already_9_16

    if convert_to_9_16:
        delogo_parts = _build_delogo_filters_cloud(w, h, delogo_mode)
        pre_split = ",".join(["format=yuv420p"] + delogo_parts) + ","
        fg_sub = _sub_stage(w, h)
        fg_chain = (
            f"[v2]{fg_sub},scale=1080:1920:force_original_aspect_ratio=decrease:flags=lanczos[fg];"
            if fg_sub
            else "[v2]scale=1080:1920:force_original_aspect_ratio=decrease:flags=lanczos[fg];"
        )
        graph = (
            f"{pre_split}split[v1][v2];"
            "[v1]scale=360:640:force_original_aspect_ratio=increase:flags=bilinear,crop=360:640,"
            "boxblur=8:3,scale=1080:1920:flags=bilinear[bg];"
            + fg_chain
            + "[bg][fg]overlay=(W-w)/2:(H-h)/2,format=yuv420p"
        )
        return graph, 1080, 1920, cover_style

    parts: list[str] = []
    max_dim = max(w, h)
    if max_dim > 3840 or min(w, h) > 2160:
        ratio = min(1080.0 / min(w, h), 1920.0 / max_dim)
        w = max(2, int(w * ratio) // 2 * 2)
        h = max(2, int(h * ratio) // 2 * 2)
        parts.append(f"scale={w}:{h}")

    delogo_parts = _build_delogo_filters_cloud(w, h, delogo_mode)
    if delogo_parts:
        parts.extend(delogo_parts)

    sub_part = _sub_stage(w, h)
    if sub_part:
        if "split" in sub_part and "format=yuv420p" not in parts and not sub_part.startswith("format=yuv420p"):
            parts.append("format=yuv420p")
        parts.append(sub_part)
    parts.append("format=yuv420p")
    return ",".join(parts), w, h, cover_style


def step7_render_video(
    input_video: str,
    final_audio_wav: str,
    output_mp4: str,
    vi_srt_path: str = "subtitles_vi.srt",
    ass_path: str = "subtitles_vi.ass",
    *,
    main_y_pct: float = 0.78,
    canvas_cfg: dict | None = None,
):
    log_step(7, 8, "Render video thành phẩm chuẩn Tool V1 (Localized Blur / Khung 9:16 / NVENC)...")

    v_info = _probe_main_video_cloud(input_video)
    orig_w, orig_h = int(v_info["width"]), int(v_info["height"])
    input_map = str(v_info["input_map"])

    safe_sub_copy: str | None = None
    filter_script_path: str | None = None
    try:
        ass_to_use = ass_path
        srt_to_use = vi_srt_path
        if ass_path and os.path.isfile(ass_path) and os.path.getsize(ass_path) > 80:
            safe_sub_copy = os.path.join(os.path.dirname(os.path.abspath(output_mp4)) or ".", "_v1_burn_sub.ass")
            if os.path.abspath(ass_path) != os.path.abspath(safe_sub_copy):
                shutil.copy2(ass_path, safe_sub_copy)
                ass_to_use = safe_sub_copy
        elif vi_srt_path and os.path.isfile(vi_srt_path) and os.path.getsize(vi_srt_path) > 10:
            safe_sub_copy = os.path.join(os.path.dirname(os.path.abspath(output_mp4)) or ".", "_v1_burn_sub.srt")
            if os.path.abspath(vi_srt_path) != os.path.abspath(safe_sub_copy):
                shutil.copy2(vi_srt_path, safe_sub_copy)
                srt_to_use = safe_sub_copy

        effective_canvas = canvas_cfg
        if effective_canvas is None and not (
            ass_to_use
            and os.path.isfile(ass_to_use)
            and "; ToolV1BlurData: " in Path(ass_to_use).read_text(encoding="utf-8-sig", errors="ignore")
        ):
            effective_canvas = {**DEFAULT_V1_RUNTIME_PROFILE["canvas"], "sub_cover_style": "solid_box"}

        filter_graph, out_w, out_h, active_cover = _build_v1_video_filter_graph(
            orig_w,
            orig_h,
            ass_path=ass_to_use,
            vi_srt_path=srt_to_use,
            main_y_pct=main_y_pct,
            canvas_cfg=effective_canvas,
        )
        print(
            f"  -> Bộ lọc V1: source={orig_w}x{orig_h} -> output={out_w}x{out_h}, cover_style={active_cover}, len={len(filter_graph)}",
            flush=True,
        )

        if len(filter_graph) > 8000:
            filter_script_path = os.path.join(os.path.dirname(os.path.abspath(output_mp4)) or ".", "_v1_render_filters.txt")
            Path(filter_script_path).write_text(filter_graph, encoding="utf-8")
            video_filter_args = ["-filter_script:v", filter_script_path]
        else:
            video_filter_args = ["-vf", filter_graph]

        video_bitrate_kbps = 8000
        maxrate_kbps = 12000
        bufsize_kbps = 16000
        src_dur = float(v_info.get("duration") or 0.0)
        if os.path.isfile(input_video) and os.path.getsize(input_video) > 100000 and src_dur > 1.0:
            src_kbps = int((os.path.getsize(input_video) * 8.0 / 1000.0) / src_dur)
            video_bitrate_kbps = min(8000, max(2500, int(src_kbps * 1.25)))
            maxrate_kbps = min(12000, max(4000, int(video_bitrate_kbps * 1.5)))
            bufsize_kbps = maxrate_kbps * 2

        b_v = f"{video_bitrate_kbps}k"
        max_v = f"{maxrate_kbps}k"
        buf_v = f"{bufsize_kbps}k"

        encoder_attempts = [
            (
                "GPU NVENC HQ (Chuẩn V1)",
                [
                    "-c:v", "h264_nvenc",
                    "-preset", "p4",
                    "-cq", "20",
                    "-b:v", b_v,
                    "-maxrate", max_v,
                    "-bufsize", buf_v,
                ],
            ),
            (
                "GPU NVENC tiêu chuẩn",
                [
                    "-c:v", "h264_nvenc",
                    "-preset", "p4",
                    "-cq", "23",
                ],
            ),
            (
                "CPU libx264 fallback",
                [
                    "-c:v", "libx264",
                    "-preset", "veryfast",
                    "-crf", "20",
                ],
            ),
        ]

        last_err: Exception | None = None
        for label, enc_flags in encoder_attempts:
            cmd = (
                ["ffmpeg", "-y", "-threads", "4", "-filter_threads", "2", "-i", input_video, "-i", final_audio_wav]
                + video_filter_args
                + ["-map", input_map, "-map", "1:a:0", "-sn", "-dn"]
                + enc_flags
                + [
                    "-pix_fmt", "yuv420p",
                    "-c:a", "aac",
                    "-b:a", "192k",
                    "-shortest",
                    "-movflags", "+faststart",
                    output_mp4,
                ]
            )
            try:
                run_cmd(cmd, f"Render video bằng {label}")
                print(f"  -> Render video hoàn tất ({label}): {output_mp4}", flush=True)
                return
            except Exception as exc:
                last_err = exc
                print(f"  [WARN] {label} không khả dụng ({exc}) -> thử cấu hình kế tiếp...", flush=True)

        raise PipelineStageError(
            "RENDER_FAILED",
            f"Render video thất bại trên cả NVENC và libx264: {last_err}",
        ) from last_err
    finally:
        for tmp_f in (filter_script_path, safe_sub_copy):
            if tmp_f and os.path.exists(tmp_f):
                try:
                    os.remove(tmp_f)
                except OSError:
                    pass


def verify_rendered_video_qc(output_mp4: str, *, expected_duration: float = 0.0) -> dict:
    """Kiểm tra ffprobe, đủ stream hình + tiếng, dung lượng và độ lệch thời lượng trên VM."""
    if not output_mp4 or not os.path.isfile(output_mp4):
        raise PipelineStageError("QC_FAILED", f"Không tìm thấy video đầu ra sau render: {output_mp4}")
    size_bytes = os.path.getsize(output_mp4)
    if size_bytes <= 1024:
        raise PipelineStageError("QC_FAILED", f"Video đầu ra quá nhỏ ({size_bytes} bytes).")

    try:
        proc = subprocess.run(
            [
                "ffprobe",
                "-v",
                "quiet",
                "-print_format",
                "json",
                "-show_format",
                "-show_streams",
                output_mp4,
            ],
            capture_output=True,
            text=True,
            timeout=20,
            shell=False,
        )
    except Exception as exc:
        raise PipelineStageError("QC_FAILED", f"Lỗi chạy ffprobe kiểm tra video đầu ra: {exc}") from exc

    if proc.returncode != 0 or not proc.stdout.strip():
        raise PipelineStageError("QC_FAILED", f"ffprobe từ chối file đầu ra {output_mp4} (rc={proc.returncode}).")

    try:
        probe = json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        raise PipelineStageError("QC_FAILED", f"ffprobe trả JSON không hợp lệ: {exc}") from exc

    fmt = probe.get("format") or {}
    streams = probe.get("streams") or []
    video_streams = [s for s in streams if s.get("codec_type") == "video"]
    audio_streams = [s for s in streams if s.get("codec_type") == "audio"]
    if not video_streams:
        raise PipelineStageError("QC_FAILED", "Video đầu ra thiếu stream hình ảnh.")
    if not audio_streams:
        raise PipelineStageError("QC_FAILED", "Video đầu ra thiếu stream âm thanh.")

    raw_dur = fmt.get("duration") or video_streams[0].get("duration") or audio_streams[0].get("duration")
    try:
        duration_s = float(raw_dur)
    except (TypeError, ValueError) as exc:
        raise PipelineStageError("QC_FAILED", "Không đọc được thời lượng video đầu ra.") from exc
    if not math.isfinite(duration_s) or duration_s <= 0.0:
        raise PipelineStageError("QC_FAILED", f"Thời lượng video đầu ra không hợp lệ: {duration_s}")

    if expected_duration > 0.0:
        tolerance = max(1.5, float(expected_duration) * 0.15)
        if abs(duration_s - float(expected_duration)) > tolerance:
            raise PipelineStageError(
                "QC_FAILED",
                f"Thời lượng output ({duration_s:.2f}s) lệch quá ngưỡng so với nguồn ({expected_duration:.2f}s).",
            )

    return {
        "size_bytes": size_bytes,
        "sha256": _file_sha256(output_mp4),
        "duration_seconds": round(duration_s, 3),
        "video_codec": video_streams[0].get("codec_name"),
        "audio_codec": audio_streams[0].get("codec_name"),
        "width": int(video_streams[0].get("width") or 0),
        "height": int(video_streams[0].get("height") or 0),
    }


def step0_download_video_from_url(url: str, output_path: str = "downloaded_video.mp4") -> str:
    """
    Tải video trực tiếp từ link (TikTok, Douyin, YouTube, Facebook, Kuaishou, link trực tiếp...)
    sử dụng yt-dlp trực tiếp trên Colab VM với tốc độ mạng datacenter 1Gbps.
    """
    safe_url = _redact_secrets_local(url)
    log_step(0, 8, f"Tải video từ liên kết trên Colab VM: {safe_url}...")
    cmd = [
        sys.executable, "-m", "yt_dlp",
        "-f", "bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best",
        "--merge-output-format", "mp4",
        "--no-playlist",
        "--no-warnings",
        "-o", output_path,
        url,
    ]
    try:
        run_cmd(cmd, "yt-dlp tải video từ URL")
        if os.path.exists(output_path) and os.path.getsize(output_path) > 1024:
            print(f"  -> Tải video thành công: {output_path} ({os.path.getsize(output_path):,} bytes)", flush=True)
            return output_path

        # Nếu yt-dlp đặt tên khác
        for fname in os.listdir("."):
            if fname.endswith(".mp4") and fname not in ("output_video.mp4",):
                print(f"  -> Tìm thấy video đã tải: {fname}", flush=True)
                return fname

        raise RuntimeError(f"Không tìm thấy file {output_path} sau khi yt-dlp hoàn tất.")
    except Exception as exc:
        safe_exc = _redact_secrets_local(str(exc))
        print(f"  [WARN] yt-dlp gặp sự cố ({safe_exc}). Thử tải trực tiếp stream qua requests...", flush=True)
        import requests
        r = requests.get(url, stream=True, timeout=90, headers={"User-Agent": "Mozilla/5.0"})
        r.raise_for_status()
        with open(output_path, "wb") as f:
            for chunk in r.iter_content(chunk_size=65536):
                f.write(chunk)
        print(f"  -> Tải trực tiếp stream thành công: {output_path}", flush=True)
        return output_path


def main():
    parser = argparse.ArgumentParser(description="AutoDub Full Pipeline V1 on Colab VM")
    parser.add_argument("--input", required=True, help="Tên file video đầu vào hoặc link URL")
    parser.add_argument("--voice", default="auto", help="Giọng đọc CapCut / Edge TTS (mặc định: auto - Tự khóa Nam/Nữ chuẩn V1)")
    parser.add_argument("--allow-no-speech", action="store_true", help="Cho phép video cố ý không có thoại")
    parser.add_argument("--allow-edge-tts-fallback", action="store_true", help="Cho phép fallback sang Edge TTS nếu CapCut TTS lỗi")
    parser.add_argument("--gemini-key", default="", help=argparse.SUPPRESS)
    args = parser.parse_args()

    start_time = time.time()
    input_target = args.input
    env_gemini_key = os.getenv("GEMINI_API_KEY", "").strip() or (args.gemini_key or "").strip()
    runtime_profile = load_v1_runtime_profile(".")
    canvas_cfg = runtime_profile["canvas"]
    audio_cfg = runtime_profile["audio"]

    print(f"=== BẮT ĐẦU PIPELINE CLOUD 8 BƯỚC CHUẨN V1 TRÊN COLAB VM ===")
    print(f"Mục tiêu đầu vào: {_redact_secrets_local(input_target)}", flush=True)
    print(
        f"Cấu hình V1: voice={args.voice}, cover={canvas_cfg['sub_cover_style']}, "
        f"aspect={canvas_cfg['aspect_ratio']}, ducking={audio_cfg['ducking_mode']}",
        flush=True,
    )

    # Tự động tải nếu đầu vào là link URL
    if input_target.startswith("http://") or input_target.startswith("https://"):
        input_video = step0_download_video_from_url(input_target, "downloaded_video.mp4")
    else:
        input_video = input_target
        if not os.path.exists(input_video):
            print(f"[ERROR] Không tìm thấy video: {input_video}", file=sys.stderr)
            sys.exit(1)

    output_audio = "input_audio.wav"
    output_srt = "subtitles.srt"
    output_vi_srt = "subtitles_vi.srt"
    output_ass = "subtitles_vi.ass"
    tts_dir = "tts_segments"
    final_audio = "final_audio.wav"
    output_video = "output_video.mp4"
    metadata_file = "result_metadata.json"

    try:
        # Bước 1: Trích âm thanh
        step1_extract_audio(input_video, output_audio)

        # Bước 2: Tách âm theo chế độ cấu hình của Tool V1 (BS-RoFormer -> Demucs GPU)
        vocals_path, bgm_path = step2_separate_vocals(
            output_audio,
            ".",
            separation_mode=str(audio_cfg.get("separation_mode") or "roformer"),
        )

        # Bước 3: ASR audio gốc; vocal là phương án thử lại có kiểm tra.
        segments = step3_asr_whisper(vocals_path, output_srt, reference_audio_path=output_audio,
                                     audit_path="asr_timing.json")
        if not segments and not args.allow_no_speech:
            raise PipelineStageError(
                "NO_SPEECH_DETECTED",
                "Whisper ASR không phát hiện lời thoại nào trong video; chặn xuất bản lồng tiếng rỗng.",
            )

        # Bước 3b: Quét OCR GPU định vị hộp che phụ đề & sửa lỗi đồng âm
        segments, main_y_pct, video_w, video_h, video_duration = step3b_ocr_and_fix_subtitles(
            input_video, segments, output_srt
        )

        # Re-read conflicting captions with their original audio; validation
        # permits supported substitutions only, retaining every cue and time.
        _write_pipeline_progress(67, 3, 8, "Đối chiếu âm thanh & phụ đề gốc trước khi dịch...")
        from caption_reviewer import review_caption_conflicts
        try:
            segments = review_caption_conflicts(input_video, segments, env_gemini_key, "caption_review.json")
        except Exception as exc:
            raise PipelineStageError("ASR_TIMING_INVALID", f"Chưa xác minh được nội dung ASR/caption: {exc}") from exc
        with open(output_srt, "w", encoding="utf-8") as f:
            for seg in segments:
                f.write(f"{seg['id']}\n{format_timestamp(seg['start'])} --> {format_timestamp(seg['end'])}\n{seg['text']}\n\n")

        # Khóa giọng đọc chuẩn Tool V1 (trước khi dịch để đồng bộ đại từ xưng hô Nam/Nữ)
        voice_lock = resolve_cloud_voice_choice(
            args.voice,
            segments,
            vocals_path=vocals_path,
            original_audio_path=output_audio,
            runtime_profile=runtime_profile,
            lock_path="voice_lock.json",
        )
        effective_voice = str(voice_lock["resolved_voice"])

        # Bước 4: Dịch phụ đề tự nhiên chuẩn V1 (Gemini AI + ngữ cảnh hình ảnh + từ điển thuật ngữ)
        segments = step4_translate_subtitles(
            segments,
            env_gemini_key,
            vi_srt_path=output_vi_srt,
            speaker_gender=str(voice_lock.get("detected_gender") or "unknown"),
            script_mode=str(audio_cfg.get("script_mode") or "default"),
            glossary=runtime_profile.get("glossary") or None,
            video_path=input_video,
            video_duration=video_duration,
        )

        # Bước 5: Tổng hợp giọng đọc TTS (CapCut TTS strict) + Cân chỉnh atempo chống đè thoại
        tts_summary = step5_generate_tts(
            segments,
            effective_voice,
            tts_dir,
            allow_no_speech=bool(args.allow_no_speech),
            allow_edge_tts_fallback=bool(args.allow_edge_tts_fallback),
            video_duration=video_duration,
            gemini_key=env_gemini_key,
        )
        # A bounded TTS retry may rephrase an overlong translation. Export the
        # exact text spoken, retaining its immutable source cue timestamps.
        with open(output_vi_srt, "w", encoding="utf-8") as f:
            for seg in segments:
                f.write(f"{seg['id']}\n{format_timestamp(seg['start'])} --> {format_timestamp(seg['end'])}\n{seg['translated_text']}\n\n")
        Path("tts_timing.json").write_text(json.dumps(tts_summary, ensure_ascii=False, indent=2), encoding="utf-8")
        Path("pipeline_segments.json").write_text(json.dumps(segments, ensure_ascii=False, indent=2), encoding="utf-8")

        # Bước 6: Hòa âm Dynamic Sidechain Ducking EBU R128 theo audio_settings của Tool V1
        step6_mix_audio_ducking_ebu_r128(
            bgm_path,
            segments,
            final_audio,
            allow_no_speech=bool(args.allow_no_speech),
            audio_cfg=audio_cfg,
        )

        # Tạo file phụ đề & làm mờ cục bộ / hộp che động (.ass) chuẩn Tool V1
        has_ass = False
        if segments:
            has_ass = build_ass_subtitles_v1(
                segments,
                output_ass,
                video_w=video_w,
                video_h=video_h,
                main_y_pct=main_y_pct,
                video_duration=video_duration,
                cover_style=str(canvas_cfg.get("sub_cover_style") or "localized_blur"),
                blur_settings=canvas_cfg,
                font_name=str(canvas_cfg.get("sub_font_name") or "Arial"),
                font_color=_hex_to_ass_color(str(canvas_cfg.get("sub_font_color") or "#000000")),
                font_weight=2 if bool(canvas_cfg.get("sub_font_bold", True)) else 1,
            )
            if not has_ass:
                raise PipelineStageError(
                    "SUBTITLE_BUILD_FAILED",
                    "Không tạo được file phụ đề & hộp che .ass chuẩn V1.",
                )

        # Bước 7: Render video thành phẩm (Localized Blur + Khung 9:16 nền mờ + NVENC)
        step7_render_video(
            input_video,
            final_audio,
            output_video,
            vi_srt_path=output_vi_srt if segments else "",
            ass_path=output_ass if has_ass else "",
            main_y_pct=main_y_pct,
            canvas_cfg=canvas_cfg,
        )

        # Bước 8: Kiểm tra QC & xuất metadata đầy đủ
        qc_probe = verify_rendered_video_qc(output_video, expected_duration=video_duration)
        elapsed = round(time.time() - start_time, 2)
        log_step(8, 8, f"Hoàn tất & đạt QC trong {elapsed} giây!")

        artifacts: dict[str, dict] = {}
        for art_name in (
            output_video,
            output_srt,
            output_vi_srt,
            output_ass,
            "voice_lock.json",
            "asr_timing.json",
            "subtitles.ocr.json",
            "caption_review.json",
            "tts_timing.json",
            "subtitle_timing.json",
            "pipeline_segments.json",
            "caption_geometry.json",
        ):
            if art_name and os.path.isfile(art_name):
                artifacts[art_name] = {
                    "size_bytes": os.path.getsize(art_name),
                    "sha256": _file_sha256(art_name),
                }

        meta = {
            "status": "completed",
            "pipeline_version": PIPELINE_VERSION,
            "elapsed_seconds": elapsed,
            "input_duration_seconds": round(float(video_duration), 3),
            "asr_timing": json.loads(Path("asr_timing.json").read_text(encoding="utf-8")),
            "segments_count": len(segments),
            "translated_count": len([s for s in segments if str(s.get("translated_text") or "").strip()]),
            "tts_verified_count": int(tts_summary.get("verified_count", 0)),
            "tts_engine": tts_summary.get("tts_engine", "capcut_tts"),
            "tts_fallback_used": bool(tts_summary.get("fallback_used", False)),
            "tts_timing": tts_summary,
            "source_caption_evidence": json.loads(Path("caption_geometry.json").read_text(encoding="utf-8")),
            "subtitle_timing": json.loads(Path("subtitle_timing.json").read_text(encoding="utf-8")) if has_ass else None,
            "no_speech_mode": bool(args.allow_no_speech and not segments),
            "voice": effective_voice,
            "voice_requested": args.voice,
            "voice_lock": voice_lock,
            "subtitle_cover_style": str(canvas_cfg.get("sub_cover_style") or "localized_blur"),
            "canvas_aspect_ratio": str(canvas_cfg.get("aspect_ratio") or "9:16"),
            "vocal_separation": str(audio_cfg.get("separation_mode") or "roformer"),
            "mixing": "ebu_r128_sidechain_ducking",
            "timing_policy": {"next_cue_gap_reserved_s": 0.04, "encoding_reserve_s": 0.01,
                              "fitted_codec": "pcm_s16le", "silence_trim_db": -45.0,
                              "pre_silence_borrow_max_s": TTS_MAX_PRE_BORROW_S,
                              "boundary_tolerance_s": 0.035, "min_speed": 1.0,
                              "natural_max_speed": TTS_NATURAL_MAX_SPEED,
                              "max_speed": TTS_EMERGENCY_MAX_SPEED},
            "mix_policy": {
                "target_lufs": -15.0,
                "true_peak_dbtp": -1.0,
                "ducking_mode": str(audio_cfg.get("ducking_mode") or "soft"),
                "bgm_volume_db": float(audio_cfg.get("bgm_volume_db", -2.0)),
                "dubbing_volume_db": float(audio_cfg.get("dubbing_volume_db", 1.0)),
            },
            "ocr_subtitle_band": round(main_y_pct, 4),
            "ass_subtitles": has_ass,
            "output_video": output_video,
            "output_video_probe": qc_probe,
            "artifacts": artifacts,
        }
        with open(metadata_file, "w", encoding="utf-8") as f:
            json.dump(meta, f, indent=2, ensure_ascii=False)

        print(f"\n{PIPELINE_RESULT_PREFIX}{json.dumps(meta, ensure_ascii=False)}", flush=True)
        print("\n[SUCCESS] Pipeline Colab Cloud V1 hoàn tất & đạt QC 100%!")

    except Exception as exc:
        safe_exc = _redact_secrets_local(str(exc), extra_secrets=(env_gemini_key,))
        err_code = exc.error_code if isinstance(exc, PipelineStageError) else "EXEC_FAILED"
        print(f"\n[FATAL ERROR] Pipeline Cloud thất bại [{err_code}]: {safe_exc}", file=sys.stderr)
        meta = {
            "status": "error",
            "pipeline_version": PIPELINE_VERSION,
            "error_code": err_code,
            "error": safe_exc,
            "elapsed_seconds": round(time.time() - start_time, 2),
        }
        with open(metadata_file, "w", encoding="utf-8") as f:
            json.dump(meta, f, indent=2, ensure_ascii=False)
        print(f"{PIPELINE_RESULT_PREFIX}{json.dumps(meta, ensure_ascii=False)}", flush=True)
        sys.exit(2)


if __name__ == "__main__":
    main()
