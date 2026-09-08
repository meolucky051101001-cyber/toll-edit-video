import os
import sys
import io
if isinstance(sys.stdout, io.TextIOWrapper):
    try: sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    except Exception: pass
if isinstance(sys.stderr, io.TextIOWrapper):
    try: sys.stderr.reconfigure(encoding='utf-8', errors='replace')
    except Exception: pass

import srt
from datetime import timedelta
from faster_whisper import WhisperModel

from .v1_model_policy import current_v1_model_policy
import logging
import time


def _cached_model_path(model_name, download_root):
    if os.path.isdir(model_name):
        return model_name
    try:
        from faster_whisper.utils import download_model
        path = download_model(model_name, cache_dir=download_root, local_files_only=True)
        if os.path.isfile(os.path.join(path, "model.bin")):
            return path
    except (OSError, ValueError):
        pass
    return model_name


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

def _transcribe_once(audio_path, model_name, num_workers, download_root=None):
    """Run one ASR model and always release its CPU/GPU memory."""

    import torch, gc
    num_threads = max((os.cpu_count() or 4) - 1, 2)
    worker_count = max(1, int(num_workers))

    model = None
    model_name = _cached_model_path(model_name, download_root)
    load_started = time.monotonic()
    if torch.cuda.is_available():
        print(
            "🚀 CUDA detected: {}. Loading Faster-Whisper {}...".format(
                torch.cuda.get_device_name(0), model_name
            )
        )
        model = WhisperModel(
            model_name,
            device="cuda",
            compute_type="int8_float16",
            num_workers=worker_count,
            download_root=download_root,
        )
    else:
        print(
            "⚡ Loading Faster-Whisper {} on CPU ({} threads)...".format(
                model_name, num_threads
            )
        )
        model = WhisperModel(
            model_name,
            device="cpu",
            compute_type="int8",
            cpu_threads=num_threads,
            download_root=download_root,
        )

    logging.getLogger(__name__).info("V1 ASR model loaded seconds=%.2f", time.monotonic()-load_started)
    try:
        segments, _info = model.transcribe(
            audio_path,
            beam_size=5,
            vad_filter=True,
            vad_parameters=dict(min_silence_duration_ms=600, threshold=0.4),
            condition_on_previous_text=False,
            temperature=[0.0, 0.2, 0.4],
            # Coarse segment timestamps can span the entire silence before the
            # next speaker. Word timestamps provide the actual audible window.
            word_timestamps=True,
        )
        
        transcribed_segments = []
        for segment in segments:
            text = segment.text.strip()
            if not text:
                continue
            start, end = _word_aligned_bounds(segment)
            transcribed_segments.append({"start": start, "end": end, "text": text})

        return _merge_short_fragments(transcribed_segments)
    finally:
        if model is not None:
            del model
        gc.collect()
        if torch.cuda.is_available():
            torch.cuda.empty_cache()
        print("🧹 Đã giải phóng bộ nhớ RAM/VRAM của Whisper AI.")


def extract_subtitles_whisper(audio_path, output_srt_path, num_workers=2):
    """Transcribe with V1's fast model, falling back to the proven model."""

    policy = current_v1_model_policy()
    failures = []
    merged_segments = None
    selected_model = None
    whisper_cache = os.path.join(policy.model_cache_directory, "faster_whisper")
    os.makedirs(whisper_cache, exist_ok=True)
    for model_name in policy.whisper_candidates:
        print(
            "Transcribing {} with Faster-Whisper {}...".format(
                audio_path, model_name
            )
        )
        try:
            merged_segments = _transcribe_once(
                audio_path,
                model_name,
                num_workers,
                download_root=whisper_cache,
            )
            selected_model = model_name
            break
        except Exception as exc:
            failures.append("{}: {}".format(model_name, exc))
            print(
                "⚠️ Faster-Whisper {} không dùng được; thử model dự phòng: {}".format(
                    model_name, exc
                )
            )

    if merged_segments is None:
        raise RuntimeError(
            "All V1 Whisper models failed: {}".format(" | ".join(failures))
        )

    print("✅ V1 ASR hoàn tất bằng Faster-Whisper {}.".format(selected_model))
    srt_segments = []
    for i, seg in enumerate(merged_segments, start=1):
        sub = srt.Subtitle(
            index=i,
            start=timedelta(seconds=seg["start"]),
            end=timedelta(seconds=seg["end"]),
            content=seg["text"].strip(),
        )
        srt_segments.append(sub)

    with open(output_srt_path, "w", encoding="utf-8") as output_file:
        output_file.write(srt.compose(srt_segments))

    return srt_segments

def save_srt(srt_segments, output_path):
    with open(output_path, "w", encoding="utf-8") as f:
        f.write(srt.compose(srt_segments, reindex=False))
