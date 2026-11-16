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
PIPELINE_VERSION = "v1_cloud_phase5"
WHISPER_MODEL = "large-v3-turbo"
ASR_MIN_AVG_LOGPROB = -1.0  # Whisper's default decoder failure threshold.


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


def log_step(step_num: int, total: int, message: str):
    print(f"\n[{step_num}/{total}] {_redact_secrets_local(message)}", flush=True)


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


def step2_separate_vocals(audio_path: str, work_dir: str) -> tuple[str, str]:
    """
    Bóc tách giọng nói gốc (Vocals) và Nhạc nền (BGM/Instrumental).
    Ưu tiên BS-RoFormer (audio-separator), fallback sang Demucs htdemucs GPU.
    Trả về (vocals_path, bgm_path).
    """
    log_step(2, 8, "Bóc tách giọng nói & nhạc nền (BS-RoFormer -> Demucs GPU)...")
    import torch

    device = "cuda" if torch.cuda.is_available() else "cpu"
    sep_dir = os.path.join(work_dir, "separated")
    os.makedirs(sep_dir, exist_ok=True)

    # 1. Thử BS-RoFormer trước nếu thư viện audio-separator khả dụng
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
            for _, text, probability in reader.readtext(cv2.cvtColor(enlarged, cv2.COLOR_BGR2RGB)):
                attempts.append({"text": str(text), "prob": float(probability), "scale": scale})
                # Re-reading can corroborate the original caption only. A newly
                # guessed string cannot overwrite ASR solely because it is confident.
                if _cjk_only_cloud(text) == caption and float(probability) > float(row.get("prob", 0)):
                    row["original_prob"] = original["prob"]
                    row["prob"] = float(probability)
                    row["recognition_method"] = "caption_crop_confirmation"
            if float(row.get("prob", 0)) >= 0.85:
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

        all_blocks = []
        for s in segments:
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

            for t in sample_times:
                cap.set(cv2.CAP_PROP_POS_MSEC, t * 1000.0)
                ok, frame = cap.read()
                if not ok or frame is None:
                    continue
                fh, fw = frame.shape[:2]
                max_dim = max(fw, fh)
                if max_dim > 1280:
                    scale = 1280.0 / max_dim
                    frame = cv2.resize(frame, (int(fw * scale), int(fh * scale)), interpolation=cv2.INTER_AREA)
                    fh, fw = frame.shape[:2]
                rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
                detections = reader.readtext(rgb)
                for bbox, txt, prob in detections:
                    if not txt or len(bbox) < 4:
                        continue
                    xs = [p[0] for p in bbox]
                    ys = [p[1] for p in bbox]
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
        print(f"  [WARN] OCR GPU gặp sự cố ({exc}). Tiếp tục không dùng hộp che tĩnh.", flush=True)
        for s in segments:
            s.setdefault("best_block", None)
            s.setdefault("tracking_blocks", [])
            s.setdefault("y_pct", main_y_pct)
            s.setdefault("max_y_pct", min(0.95, main_y_pct + 0.06))
            s.setdefault("ocr_mask_status", "no_caption_evidence")

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


def step4_translate_subtitles(segments: list[dict], gemini_key: str, vi_srt_path: str = "subtitles_vi.srt") -> list[dict]:
    log_step(4, 8, "Dịch phụ đề sang tiếng Việt tự nhiên (Gemini AI)...")
    if not segments:
        return segments

    actual_key = (gemini_key or os.getenv("GEMINI_API_KEY", "")).strip()
    if not actual_key:
        raise PipelineStageError(
            "TRANSLATION_FAILED",
            "Thiếu GEMINI_API_KEY nên không thể dịch phụ đề sang tiếng Việt.",
        )

    candidate_models = ["gemini-3-flash-preview", "gemini-3.6-flash", "gemini-3.5-flash-lite"]
    max_attempts_per_model = 2
    trans_map: dict[int, str] | None = None
    last_error = "Không nhận được phản hồi dịch hợp lệ."

    try:
        from google import genai
        from google.genai import types

        client = genai.Client(api_key=actual_key)
        input_texts = [{"id": int(s["id"]), "text": s["text"],
                        "duration_seconds": round(float(s["end"]) - float(s["start"]), 3)} for s in segments]
        prompt = (
            "Bạn là chuyên gia dịch thuật và lồng tiếng video chuyên nghiệp. "
            "Đọc TOÀN BỘ danh sách để xác định ngữ cảnh và hành động trước khi dịch từng cue. "
            "Dịch thuật ngữ/nghĩa bóng theo ngữ cảnh và tên vật dụng thông dụng tiếng Việt; tránh dịch sát từng chữ gây sai hoặc tối nghĩa. "
            "Dịch ĐẦY ĐỦ từng câu thoại sau từ tiếng Trung sang tiếng Việt chuẩn, tự nhiên, văn phong đời thực, súc tích để đọc lồng tiếng. "
            "Không được bỏ sót, không gộp hay trùng id, không giữ chữ Trung Quốc. "
            "Giữ đúng nghĩa và mọi số lượng/đơn vị gốc; không quy đổi tiền, không tự thêm đơn vị tiền hay giá. "
            "Không thêm tính chất, xưng hô, lời cảm thán hoặc quảng cáo mà nguồn không có. "
            "Không bỏ tên riêng, tên chất liệu/loại giấy, hướng thao tác hoặc mệnh đề để rút ngắn. "
            "Tên riêng Trung Quốc phải phiên âm Latin phù hợp, không thay bằng 'giấy' hay 'vật liệu' chung chung. "
            "Mỗi câu phải ngắn gọn để đọc trong duration_seconds; dùng cách nói tối giản nhưng giữ đủ ý. "
            "BẮT BUỘC giữ nguyên định dạng JSON list: [{\"id\": ..., \"text\": \"...\"}]:\n"
            + json.dumps(input_texts, ensure_ascii=False)
        )

        for model_name in candidate_models:
            for attempt_idx in range(1, max_attempts_per_model + 1):
                try:
                    response = client.models.generate_content(
                        model=model_name,
                        contents=prompt,
                        config=types.GenerateContentConfig(response_mime_type="application/json"),
                    )
                    raw_json = _strip_json_fences(getattr(response, "text", "") or "")
                    if not raw_json:
                        raise PipelineStageError("TRANSLATION_FAILED", f"Model {model_name} trả về phản hồi rỗng.")
                    translated_data = json.loads(raw_json)
                    trans_map = validate_translation_items(segments, translated_data)
                    print(
                        f"  -> Đã dịch & xác thực đủ {len(segments)} câu thoại bằng {model_name} (lần {attempt_idx}).",
                        flush=True,
                    )
                    break
                except Exception as m_err:
                    safe_m_err = _redact_secrets_local(str(m_err), extra_secrets=(actual_key,))
                    last_error = f"{model_name} (lần {attempt_idx}): {safe_m_err}"
                    print(f"  [WARN] Dịch thất bại với {last_error}", flush=True)
                    if attempt_idx < max_attempts_per_model:
                        time.sleep(0.5 * attempt_idx)
            if trans_map is not None:
                break
    except PipelineStageError:
        raise
    except Exception as exc:
        safe_exc = _redact_secrets_local(str(exc), extra_secrets=(actual_key,))
        raise PipelineStageError("TRANSLATION_FAILED", f"Lỗi khởi tạo Gemini Client: {safe_exc}") from exc

    if trans_map is None:
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
            while time.monotonic() < deadline:
                time.sleep(0.35)
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


def _shorten_translation_for_timing(segment: dict, window_s: float, gemini_key: str) -> str:
    """Rephrase from source for a bounded TTS retry; never crop speech to fit."""
    if not gemini_key:
        raise PipelineStageError("TTS_GENERATION_FAILED", "TTS vượt khung; thiếu khóa để dịch lại súc tích.")
    from google import genai
    from google.genai import types
    client = genai.Client(api_key=gemini_key)
    prompt = (
        "Dịch lại câu nguồn sang tiếng Việt thật ngắn gọn để lồng tiếng. Giữ ĐẦY ĐỦ ý, phủ định, số và đơn vị; "
        "không tự đổi tiền, thêm tính từ/xưng hô/cảm thán. Không cắt mất mệnh đề, tên riêng/chất liệu hoặc hướng thao tác. "
        "Bản dịch cũ quá dài cho khung. Trả JSON list [{\"id\": ..., \"text\": \"...\"}].\n"
        + json.dumps({"id": segment["id"], "source": segment["text"],
                      "previous_translation": segment["translated_text"], "target_seconds": round(window_s, 3),
                      "target_vietnamese_words": max(2, int(window_s * 4.0))}, ensure_ascii=False)
    )
    last_error = "No valid concise translation"
    for model in ("gemini-3-flash-preview", "gemini-3.6-flash", "gemini-3.5-flash-lite"):
        for attempt in range(2):
            try:
                response = client.models.generate_content(model=model, contents=prompt,
                    config=types.GenerateContentConfig(response_mime_type="application/json", temperature=0.0))
                data = json.loads(_strip_json_fences(getattr(response, "text", "") or ""))
                return validate_translation_items([segment], data)[int(segment["id"])]
            except Exception as exc:
                last_error = _redact_secrets_local(str(exc), extra_secrets=(gemini_key,))
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

    async def _run_all():
        nonlocal fallback_used
        for s in segments:
            t_text = str(s.get("translated_text") or "").strip()
            if not t_text:
                raise PipelineStageError(
                    "TRANSLATION_FAILED",
                    f"Câu #{s['id']} thiếu bản dịch tiếng Việt hợp lệ trước khi tạo TTS.",
                )
            out_path = os.path.join(tts_dir, f"seg_{int(s['id']):04d}.mp3")
            s["tts_path"] = out_path

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

    asyncio.run(_run_all())

    # Nén tốc độ FFmpeg atempo chống đè thoại kế tiếp chuẩn V1
    try:
        from v1_timing_solver import build_safe_atempo_filter

        adjusted = 0
        cue_audits = []
        for idx, s in enumerate(segments):
            tts_p = s.get("tts_path")
            raw_dur = validate_tts_audio_file(str(tts_p or ""))
            next_boundary = (float(segments[idx + 1]["start"]) - 0.04 if idx + 1 < len(segments)
                             else float(video_duration) if video_duration else float(s["end"]) + 0.5)
            boundary = min(next_boundary, float(s["end"]) + 0.5)
            win_dur = boundary - float(s["start"])
            if win_dur <= 0.05:
                raise PipelineStageError("TTS_GENERATION_FAILED", f"Cue #{s['id']} không có khung đọc hợp lệ.")
            history = [{"text": s["translated_text"], "raw_duration": raw_dur}]
            # Keep 10 ms for atempo rounding. Fitted output is lossless PCM,
            # so MP3 encoder padding does not require another 40 ms reserve.
            # The separate 40 ms gap before the next cue is still preserved.
            fit_window = max(0.05, win_dur - 0.01)
            for retry in range(2):
                if raw_dur / fit_window <= 1.5:
                    break
                replacement = _shorten_translation_for_timing(s, win_dur, gemini_key or os.getenv("GEMINI_API_KEY", ""))
                if not replacement or replacement == s["translated_text"]:
                    break
                s["translated_text"] = replacement
                tts_p = os.path.join(tts_dir, f"seg_{int(s['id']):04d}.mp3")
                s["tts_path"] = tts_p
                if not is_capcut or not generate_capcut_tts_single(replacement, capcut_voice, str(tts_p)):
                    raise PipelineStageError("TTS_GENERATION_FAILED", f"TTS dịch lại thất bại ở cue #{s['id']}.")
                raw_dur = validate_tts_audio_file(str(tts_p))
                history.append({"text": replacement, "raw_duration": raw_dur})
            speed = max(1.0, raw_dur / fit_window)
            if speed > 1.5 + 1e-6:
                raise PipelineStageError("TTS_GENERATION_FAILED",
                    f"Cue #{s['id']} cần {speed:.2f}x sau dịch lại; chặn cắt/đè lời (giới hạn 1.5x).")
            original_duration = raw_dur
            if speed > 1.01:
                af = build_safe_atempo_filter(speed)
                if af:
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
            s["tts_duration"] = raw_dur
            s["tts_speed"] = speed
            if float(s["start"]) + raw_dur > boundary + 0.035:
                raise PipelineStageError("TTS_GENERATION_FAILED", f"TTS cue #{s['id']} vẫn vượt biên sau atempo.")
            cue_audits.append({"id": s["id"], "source_text": s["text"], "text": s["translated_text"],
                "start": s["start"], "source_end": s["end"], "boundary": boundary,
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
):
    """
    Hòa âm Dynamic Ducking chuẩn EBU R128:
    - Chặn xuất bản chỉ có nhạc nền khi video có thoại nhưng thiếu TTS.
    - Nhạc nền (BGM sạch từ Demucs) tự động nén xuống khi có lời đọc.
    - Chuẩn hóa âm lượng EBU R128 (-15.0 LUFS, True Peak -1.0 dBTP).
    """
    log_step(6, 8, "Hòa âm Dynamic Ducking & Chuẩn hóa EBU R128 (Studio Quality)...")

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
            validate_tts_audio_file(str(tts_file or ""))
        except ValueError:
            missing_cues.append(int(s["id"]))
            continue
        dubbing_audio_files.append({
            "path": str(tts_file),
            "start": float(s["start"]),
            "index": int(s["id"]),
        })

    if missing_cues or len(dubbing_audio_files) != len(segments):
        raise PipelineStageError(
            "TTS_GENERATION_FAILED",
            f"Thiếu hoặc hỏng file TTS cho {len(missing_cues)}/{len(segments)} câu thoại ({missing_cues[:10]}).",
        )

    # Thử hòa âm bằng module mixer chuẩn Tool V1 (FFmpeg native filter graph)
    mixed_ok = False
    try:
        from mixer import FFmpegMixSettings, mix_audio_ffmpeg

        settings = FFmpegMixSettings(
            background_gain_db=-2.0,
            voice_gain_db=1.0,
            duck_threshold=0.025,
            duck_ratio=8.0,
            duck_attack_ms=20.0,
            duck_release_ms=300.0,
            target_lufs=-15.0,
            true_peak_dbtp=-1.0,
            loudness_range=11.0,
            output_sample_rate=48000,
        )

        print(f"  -> Chạy FFmpeg Native Sidechain Ducking với {len(dubbing_audio_files)} câu thoại...", flush=True)
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
) -> bool:
    from subtitle_timing import SubtitleTimingError, generate_cloud_ass
    try:
        audit = generate_cloud_ass(segments, ass_path, video_w=video_w, video_h=video_h,
                                   main_y_pct=main_y_pct, video_duration=video_duration)
        Path(ass_path).with_name("subtitle_timing.json").write_text(
            json.dumps(audit, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"  -> ASS source text/mask coverage verified: {ass_path}", flush=True)
        return True
    except SubtitleTimingError as exc:
        raise PipelineStageError("SUBTITLE_BUILD_FAILED", f"ASS timing/coverage failed: {exc}") from exc


def step7_render_video(
    input_video: str,
    final_audio_wav: str,
    output_mp4: str,
    vi_srt_path: str = "subtitles_vi.srt",
    ass_path: str = "subtitles_vi.ass",
):
    log_step(7, 8, "Render video thành phẩm (Hộp che phụ đề động chuẩn V1 & âm thanh NVENC)...")

    # Ưu tiên tuyệt đối render file .ass động bo góc chuẩn V1 (không dùng drawbox tĩnh toàn màn)
    video_filter_args = []
    if ass_path and os.path.exists(ass_path) and os.path.getsize(ass_path) > 100:
        escaped_ass = str(ass_path).replace("\\", "/").replace(":", "\\:")
        video_filter_args = ["-filter_complex", f"[0:v]subtitles='{escaped_ass}'[vout]", "-map", "[vout]"]
        print("  -> Kích hoạt bộ lọc .ass hộp che bo góc động & phụ đề tiếng Việt chuẩn V1.", flush=True)
    elif vi_srt_path and os.path.exists(vi_srt_path) and os.path.getsize(vi_srt_path) > 10:
        escaped_srt = str(vi_srt_path).replace("\\", "/").replace(":", "\\:")
        vf = f"subtitles='{escaped_srt}':force_style='FontSize=15,PrimaryColour=&H0000FFFF,OutlineColour=&H00000000,BorderStyle=1,Outline=2,Shadow=1,MarginV=60'"
        video_filter_args = ["-filter_complex", f"[0:v]{vf}[vout]", "-map", "[vout]"]
        print("  -> Fallback lồng phụ đề SRT tiếng Việt (không dùng hộp đen toàn khung).", flush=True)
    else:
        video_filter_args = ["-map", "0:v:0"]

    # 1. Thử render siêu tốc với phần cứng NVIDIA GPU (h264_nvenc) trên Colab T4/L4/A100
    # BẮT BUỘC: -pix_fmt yuv420p và -movflags +faststart để đảm bảo 100% video phát được trên mọi máy tính và điện thoại
    nvenc_cmd = [
        "ffmpeg", "-y",
        "-i", input_video,
        "-i", final_audio_wav,
    ] + video_filter_args + [
        "-map", "1:a:0",
        "-c:v", "h264_nvenc",
        "-preset", "p4",
        "-cq", "23",
        "-pix_fmt", "yuv420p",
        "-c:a", "aac",
        "-b:a", "192k",
        "-shortest",
        "-movflags", "+faststart",
        output_mp4
    ]
    try:
        run_cmd(nvenc_cmd, "Render video bằng GPU NVENC siêu tốc")
        print(f"  -> Render video bằng GPU NVENC hoàn tất: {output_mp4}", flush=True)
        return
    except Exception as e:
        print(f"  [WARN] h264_nvenc không khả dụng ({e}) -> Fallback sang CPU libx264...", flush=True)

    # 2. Fallback sang CPU libx264 (preset veryfast + chuẩn yuv420p)
    cpu_cmd = [
        "ffmpeg", "-y",
        "-i", input_video,
        "-i", final_audio_wav,
    ] + video_filter_args + [
        "-map", "1:a:0",
        "-c:v", "libx264",
        "-preset", "veryfast",
        "-crf", "22",
        "-pix_fmt", "yuv420p",
        "-c:a", "aac",
        "-b:a", "192k",
        "-shortest",
        "-movflags", "+faststart",
        output_mp4
    ]
    try:
        run_cmd(cpu_cmd, "Render video bằng CPU libx264")
    except Exception as exc:
        raise PipelineStageError("RENDER_FAILED", f"Render video thất bại trên cả NVENC và libx264: {exc}") from exc
    print(f"  -> Render video bằng CPU hoàn tất: {output_mp4}", flush=True)


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
    parser.add_argument("--voice", default="BV562_streaming", help="Giọng đọc CapCut / Edge TTS (mặc định: BV562_streaming - CapCut Mai)")
    parser.add_argument("--allow-no-speech", action="store_true", help="Cho phép video cố ý không có thoại")
    parser.add_argument("--allow-edge-tts-fallback", action="store_true", help="Cho phép fallback sang Edge TTS nếu CapCut TTS lỗi")
    parser.add_argument("--gemini-key", default="", help=argparse.SUPPRESS)
    args = parser.parse_args()

    start_time = time.time()
    input_target = args.input
    env_gemini_key = os.getenv("GEMINI_API_KEY", "").strip() or (args.gemini_key or "").strip()

    print(f"=== BẮT ĐẦU PIPELINE CLOUD 8 BƯỚC CHUẨN V1 TRÊN COLAB VM ===")
    print(f"Mục tiêu đầu vào: {_redact_secrets_local(input_target)}", flush=True)
    print(f"Giọng đọc: {args.voice}", flush=True)

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

        # Bước 2: Tách âm (Demucs GPU)
        vocals_path, bgm_path = step2_separate_vocals(output_audio, ".")

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
        from caption_reviewer import review_caption_conflicts
        try:
            segments = review_caption_conflicts(input_video, segments, env_gemini_key, "caption_review.json")
        except Exception as exc:
            raise PipelineStageError("ASR_TIMING_INVALID", f"Chưa xác minh được nội dung ASR/caption: {exc}") from exc
        with open(output_srt, "w", encoding="utf-8") as f:
            for seg in segments:
                f.write(f"{seg['id']}\n{format_timestamp(seg['start'])} --> {format_timestamp(seg['end'])}\n{seg['text']}\n\n")

        # Bước 4: Dịch phụ đề tự nhiên (Gemini AI) - chặn thiếu/sót/chữ gốc
        segments = step4_translate_subtitles(segments, env_gemini_key, vi_srt_path=output_vi_srt)

        # Bước 5: Tổng hợp giọng đọc TTS (CapCut TTS strict) + Cân chỉnh atempo chống đè thoại
        tts_summary = step5_generate_tts(
            segments,
            args.voice,
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

        # Bước 6: Hòa âm Dynamic Sidechain Ducking EBU R128
        step6_mix_audio_ducking_ebu_r128(
            bgm_path,
            segments,
            final_audio,
            allow_no_speech=bool(args.allow_no_speech),
        )

        # Tạo file phụ đề & hộp che bo góc động (.ass) chuẩn Tool V1
        has_ass = False
        if segments:
            has_ass = build_ass_subtitles_v1(
                segments,
                output_ass,
                video_w=video_w,
                video_h=video_h,
                main_y_pct=main_y_pct,
                video_duration=video_duration,
            )
            if not has_ass:
                raise PipelineStageError(
                    "SUBTITLE_BUILD_FAILED",
                    "Không tạo được file phụ đề & hộp che .ass chuẩn V1.",
                )

        # Bước 7: Render video thành phẩm
        step7_render_video(
            input_video,
            final_audio,
            output_video,
            vi_srt_path=output_vi_srt if segments else "",
            ass_path=output_ass if has_ass else "",
        )

        # Bước 8: Kiểm tra QC & xuất metadata đầy đủ
        qc_probe = verify_rendered_video_qc(output_video, expected_duration=video_duration)
        elapsed = round(time.time() - start_time, 2)
        log_step(8, 8, f"Hoàn tất & đạt QC trong {elapsed} giây!")

        artifacts: dict[str, dict] = {}
        for art_name in (output_video, output_srt, output_vi_srt, output_ass,
                         "asr_timing.json", "subtitles.ocr.json", "caption_review.json", "tts_timing.json", "subtitle_timing.json", "pipeline_segments.json"):
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
            "subtitle_timing": json.loads(Path("subtitle_timing.json").read_text(encoding="utf-8")) if has_ass else None,
            "no_speech_mode": bool(args.allow_no_speech and not segments),
            "voice": args.voice,
            "vocal_separation": "demucs_htdemucs",
            "mixing": "ebu_r128_sidechain_ducking",
            "timing_policy": {"next_cue_gap_reserved_s": 0.04, "encoding_reserve_s": 0.01,
                              "fitted_codec": "pcm_s16le",
                              "boundary_tolerance_s": 0.035, "min_speed": 1.0, "max_speed": 1.5},
            "mix_policy": {"target_lufs": -15.0, "true_peak_dbtp": -1.0, "duck_ratio": 8.0},
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
