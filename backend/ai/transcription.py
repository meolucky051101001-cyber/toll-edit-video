import os
import sys
import io
import logging
if isinstance(sys.stdout, io.TextIOWrapper):
    try: sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    except Exception: pass
if isinstance(sys.stderr, io.TextIOWrapper):
    try: sys.stderr.reconfigure(encoding='utf-8', errors='replace')
    except Exception: pass

import srt
from datetime import timedelta
import threading

from .model_policy import current_model_policy
from .model_runtime import ModelRuntimeError, run_model_stage, runtime_module_available


logger = logging.getLogger(__name__)


def _word_aligned_bounds(segment):
    """Return the audible speech bounds instead of Whisper's coarse chunk bounds."""

    valid_words = []
    for word in getattr(segment, "words", None) or []:
        start = getattr(word, "start", None)
        end = getattr(word, "end", None)
        if start is None or end is None:
            continue
        start = float(start)
        end = float(end)
        if start >= 0 and end > start:
            valid_words.append(
                {
                    "start": start,
                    "end": end,
                    "text": str(getattr(word, "word", "") or ""),
                    "probability": float(
                        getattr(word, "probability", 0.0) or 0.0
                    ),
                }
            )
    if valid_words:
        clusters = [[valid_words[0]]]
        for word in valid_words[1:]:
            if word["start"] - clusters[-1][-1]["end"] > 1.25:
                clusters.append([word])
            else:
                clusters[-1].append(word)

        # Whisper occasionally attaches the first token of a sentence to the
        # previous speech window, then leaves several seconds of silence before
        # the remaining words. Keep the dominant contiguous word cluster while
        # retaining the complete recognized text for translation.
        best_cluster = max(
            clusters,
            key=lambda cluster: (
                sum(
                    1
                    for word in cluster
                    for character in word["text"]
                    if not character.isspace()
                ),
                len(cluster),
                sum(word["probability"] for word in cluster),
                cluster[-1]["end"],
            ),
        )
        start = best_cluster[0]["start"]
        end = best_cluster[-1]["end"]
        if len(clusters) > 1:
            recognized_characters = sum(
                1
                for character in str(getattr(segment, "text", "") or "")
                if not character.isspace()
            )
            minimum_window = min(1.2, max(0.65, recognized_characters * 0.16))
            start = max(0.0, min(start, end - minimum_window))
        return start, end
    return float(segment.start), float(segment.end)


def _merge_short_fragments(segments):
    """Merge only tiny ASR fragments, never full adjacent subtitle lines."""

    merged = []
    current = None
    for item in segments:
        if current is None:
            current = dict(item)
            continue
        gap = float(item["start"]) - float(current["end"])
        current_duration = float(current["end"]) - float(current["start"])
        item_duration = float(item["end"]) - float(item["start"])
        combined_duration = float(item["end"]) - float(current["start"])
        is_tiny_fragment = current_duration <= 0.65 or item_duration <= 0.65
        if -0.05 <= gap <= 0.12 and combined_duration <= 1.25 and is_tiny_fragment:
            current["end"] = item["end"]
            current["text"] += " " + item["text"]
        else:
            merged.append(current)
            current = dict(item)
    if current is not None:
        merged.append(current)
    return merged

def _group_fast_speech_windows(segments, max_seconds=4.8, max_characters=48):
    """Bound adjacent Whisper lines into speech windows before translation.

    Keep every recognized word and the audible outer bounds; never bridge a
    significant pause or overlap. ASS still handles sentence display changes.
    Only the opt-in fast profile uses this, leaving raw Whisper timing intact.
    """
    grouped = []
    for item in segments:
        if grouped:
            previous = grouped[-1]
            gap = float(item["start"]) - float(previous["end"])
            duration = float(item["end"]) - float(previous["start"])
            text = _join_aligned_tokens(previous["text"], item["text"], join_cjk_lines_with_comma=True)
            if (0 <= gap <= 0.20 and duration <= max_seconds
                    and len(text.replace(" ", "")) <= max_characters):
                previous["end"] = item["end"]
                previous["text"] = text
                continue
        grouped.append(dict(item))
    return grouped


def _join_aligned_tokens(left, right, join_cjk_lines_with_comma=False):
    left = str(left or "")
    right = str(right or "")
    if not left:
        return right
    if not right:
        return left
    cjk = lambda value: any("\u3400" <= character <= "\u9fff" for character in value)
    if join_cjk_lines_with_comma and cjk(left[-1:]) and cjk(right[:1]):
        if not left.endswith(("，", "。", "！", "？", "；", "：", "、", ",", ".", "!", "?", ";", ":")):
            return left + "，" + right
    if cjk(left[-1:]) or cjk(right[:1]) or right[:1] in "，。！？；：、,.!?;:":
        return left + right
    if left.endswith((" ", "\n")) or right.startswith((" ", "\n")):
        return left + right
    return left + " " + right


def _aligned_units_to_segments(units):
    """Group Qwen character/word timestamps into subtitle-sized speech windows."""

    normalized = []
    for unit in units or []:
        try:
            start = max(0.0, float(unit.get("start", 0.0)))
            end = float(unit.get("end", start))
        except (TypeError, ValueError, AttributeError):
            continue
        text = str(unit.get("text", "") or "")
        if not text.strip() or end <= start:
            continue
        normalized.append({"start": start, "end": end, "text": text})
    normalized.sort(key=lambda item: (item["start"], item["end"]))

    grouped = []
    current = None
    sentence_end = set("。！？!?；;")
    for unit in normalized:
        if current is None:
            current = dict(unit)
            continue
        gap = unit["start"] - current["end"]
        visible_length = sum(
            1 for character in current["text"] if not character.isspace()
        )
        duration = unit["end"] - current["start"]
        should_split_before = gap > 0.8 or duration > 6.0 or visible_length >= 24
        if should_split_before:
            grouped.append(current)
            current = dict(unit)
            continue
        current["text"] = _join_aligned_tokens(current["text"], unit["text"])
        current["end"] = max(current["end"], unit["end"])
        if current["text"].rstrip()[-1:] in sentence_end:
            grouped.append(current)
            current = None
    if current is not None:
        grouped.append(current)
    return grouped


def _write_srt_segments(raw_segments, output_srt_path):
    subtitles = []
    for index, segment in enumerate(raw_segments, start=1):
        content = str(segment["text"] or "").strip()
        if not content:
            continue
        subtitles.append(
            srt.Subtitle(
                index=index,
                start=timedelta(seconds=float(segment["start"])),
                end=timedelta(seconds=float(segment["end"])),
                content=content,
            )
        )
    if not subtitles:
        raise RuntimeError("ASR produced no usable subtitle segments")
    with open(output_srt_path, "w", encoding="utf-8") as handle:
        handle.write(srt.compose(subtitles))
    return subtitles


def _extract_subtitles_qwen(audio_path, output_srt_path, policy):
    print(
        "Transcribing {} with {} + forced alignment...".format(
            audio_path, policy.qwen_asr_model
        )
    )
    result = run_model_stage(
        "qwen_asr",
        {
            "input_audio": str(audio_path),
            "model_name": policy.qwen_asr_model,
            "aligner_name": policy.qwen_aligner_model,
            "language": policy.qwen_language,
            "chunk_seconds": 240.0,
            "overlap_seconds": 0.75,
            "max_new_tokens": 4096,
        },
        timeout_seconds=float(os.getenv("ASR_MODEL_TIMEOUT_SECONDS", "7200")),
        policy=policy,
    )
    grouped = _aligned_units_to_segments(result.get("timestamps", []))
    subtitles = _write_srt_segments(grouped, output_srt_path)
    print(
        "Qwen3-ASR completed: {} subtitle windows, language={}.".format(
            len(subtitles), result.get("language", "unknown")
        )
    )
    return subtitles


_cached_whisper_model = None
_cached_whisper_key = None
_whisper_lock = threading.Lock()


def release_whisper_model():
    """Giải phóng mô hình Whisper khỏi RAM/VRAM khi cần nhường tài nguyên cho mô hình khác."""
    global _cached_whisper_model, _cached_whisper_key
    with _whisper_lock:
        if _cached_whisper_model is not None:
            del _cached_whisper_model
            _cached_whisper_model = None
            _cached_whisper_key = None
            try:
                import gc
                import torch
                gc.collect()
                if torch.cuda.is_available():
                    torch.cuda.empty_cache()
            except Exception:
                pass
            print("🧹 Đã giải phóng bộ nhớ RAM/VRAM của Whisper AI.")


def get_or_load_whisper_model(model_name: str, num_workers: int = 1):
    """Lấy mô hình Whisper đã nạp trong bộ nhớ hoặc nạp mới nếu chưa có."""
    global _cached_whisper_model, _cached_whisper_key
    from faster_whisper import WhisperModel
    import torch

    device = "cuda" if torch.cuda.is_available() else "cpu"
    compute_type = "int8_float16" if device == "cuda" else "int8"
    cpu_threads = max((os.cpu_count() or 4) - 1, 2)
    worker_count = max(1, int(num_workers))
    cache_key = (model_name, device, compute_type, worker_count if device == "cuda" else cpu_threads)

    with _whisper_lock:
        if _cached_whisper_model is not None and _cached_whisper_key == cache_key:
            return _cached_whisper_model

        if device == "cuda":
            print(
                "🚀 CUDA detected: {}. Loading Whisper {}...".format(
                    torch.cuda.get_device_name(0), model_name
                )
            )
            model = WhisperModel(
                model_name,
                device="cuda",
                compute_type=compute_type,
                num_workers=worker_count,
            )
        else:
            print("⚡ Loading Whisper {} on CPU ({} threads)...".format(model_name, cpu_threads))
            model = WhisperModel(
                model_name,
                device="cpu",
                compute_type=compute_type,
                cpu_threads=cpu_threads,
            )

        keep_loaded = os.getenv("AUTODUB_KEEP_WHISPER_LOADED", "1").strip().lower() not in ("0", "false", "no")
        if keep_loaded:
            _cached_whisper_model = model
            _cached_whisper_key = cache_key
        return model


def _extract_subtitles_faster_whisper(
    audio_path, output_srt_path, num_workers=2, model_name="large-v3", group_speech_windows=False,
    initial_prompt=None, keep_loaded=None
):
    print("Transcribing {} with Faster-Whisper {}...".format(audio_path, model_name))
    model = get_or_load_whisper_model(model_name=model_name, num_workers=num_workers)

    if initial_prompt is None:
        initial_prompt = "这是一段带有标点符号的中文视频，包含逗号，句号！"

    try:
        segments, info = model.transcribe(
            audio_path, 
            beam_size=5, 
            vad_filter=True, 
            vad_parameters=dict(min_silence_duration_ms=600, threshold=0.4),
            condition_on_previous_text=False,
            temperature=[0.0, 0.2, 0.4],
            # Coarse segment timestamps can span the entire silence before the
            # next speaker. Word timestamps provide the actual audible window.
            word_timestamps=True,
            initial_prompt=initial_prompt,
        )
        
        transcribed_segments = []
        for segment in segments:
            text = segment.text.strip()
            if not text:
                continue
            start, end = _word_aligned_bounds(segment)
            transcribed_segments.append({"start": start, "end": end, "text": text})

        merged_segments = _merge_short_fragments(transcribed_segments)
        if group_speech_windows:
            merged_segments = _group_fast_speech_windows(merged_segments)
        
        return _write_srt_segments(merged_segments, output_srt_path)
    finally:
        should_keep = (
            keep_loaded
            if keep_loaded is not None
            else (os.getenv("AUTODUB_KEEP_WHISPER_LOADED", "1").strip().lower() not in ("0", "false", "no"))
        )
        if not should_keep:
            release_whisper_model()


def extract_subtitles_whisper(audio_path, output_srt_path, num_workers=2):
    """Run the strongest configured ASR and retain Whisper as a safe fallback.

    The public name is preserved because existing V1/V2 callers import it.
    """

    policy = current_model_policy()
    if policy.asr_backend in {"auto", "qwen3"}:
        if runtime_module_available("qwen_asr", policy):
            try:
                return _extract_subtitles_qwen(audio_path, output_srt_path, policy)
            except (ModelRuntimeError, RuntimeError, OSError, ValueError) as exc:
                logger.warning(
                    "Qwen3-ASR failed; falling back to Faster-Whisper %s: %s",
                    policy.whisper_model,
                    exc,
                )
        else:
            logger.warning(
                "Qwen3-ASR runtime unavailable; using Faster-Whisper %s",
                policy.whisper_model,
            )
    return _extract_subtitles_faster_whisper(
        audio_path,
        output_srt_path,
        num_workers=num_workers,
        model_name=policy.whisper_model,
        group_speech_windows=getattr(policy, "speed_profile", "quality") == "fast",
    )

def save_srt(srt_segments, output_path):
    with open(output_path, "w", encoding="utf-8") as f:
        f.write(srt.compose(srt_segments, reindex=False))
