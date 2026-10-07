import os
import sys
import io
from pathlib import Path
if isinstance(sys.stdout, io.TextIOWrapper):
    try: sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    except Exception: pass
if isinstance(sys.stderr, io.TextIOWrapper):
    try: sys.stderr.reconfigure(encoding='utf-8', errors='replace')
    except Exception: pass

import re
import srt
from datetime import timedelta
import threading

from .model_policy import current_model_policy
from .model_runtime import ModelRuntimeError, run_model_stage, runtime_module_available


logger = logging.getLogger(__name__)

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

def _transcribe_raw_segments(model, target_audio):
    segments, _info = model.transcribe(
        target_audio,
        beam_size=5,
        vad_filter=True,
        vad_parameters=dict(min_silence_duration_ms=600, threshold=0.4),
        condition_on_previous_text=False,
        temperature=[0.0, 0.2, 0.4],
        word_timestamps=True,
    )
    transcribed_segments = []
    for segment in segments:
        text = segment.text.strip()
        if not text:
            continue
        words = getattr(segment, "words", None)
        duration = float(segment.end) - float(segment.start)
        if words and duration > 6.0:
            sub_chunks = []
            cur = []
            for w in words:
                cur.append(w)
                cur_dur = float(cur[-1].end) - float(cur[0].start)
                w_text = str(getattr(w, "word", "") or "")
                is_end = any(p in w_text for p in "。！？!?")
                is_comma = any(p in w_text for p in "，,；;") and cur_dur >= 3.0
                if is_end or is_comma:
                    txt = "".join(str(getattr(x, "word", "") or "") for x in cur).strip()
                    if txt:
                        sub_chunks.append({"start": float(cur[0].start), "end": float(cur[-1].end), "text": txt})
                    cur = []
            if cur:
                txt = "".join(str(getattr(x, "word", "") or "") for x in cur).strip()
                if txt:
                    sub_chunks.append({"start": float(cur[0].start), "end": float(cur[-1].end), "text": txt})
            if len(sub_chunks) > 1:
                transcribed_segments.extend(sub_chunks)
                continue
        start, end = _word_aligned_bounds(segment)
        transcribed_segments.append({"start": start, "end": end, "text": text})
    return transcribed_segments


def _transcribe_once(audio_path, model_name, num_workers, download_root=None, original_audio_path=None, chunking=None, chunk_size_s=240.0, overlap_s=0.75):
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
            num_workers=1,
            download_root=download_root,
        )
    else:
        raise RuntimeError("V1 ASR requires CUDA; CPU fallback is disabled.")

    logging.getLogger(__name__).info("V1 ASR model loaded seconds=%.2f", time.monotonic()-load_started)
    try:
        try:
            from .v1_asr_chunking import (
                get_audio_duration_seconds,
                chunk_audio_intervals,
                deduplicate_boundary_segments,
                slice_audio_chunk,
            )
        except ImportError:
            from ai.v1_asr_chunking import (
                get_audio_duration_seconds,
                chunk_audio_intervals,
                deduplicate_boundary_segments,
                slice_audio_chunk,
            )

        total_dur = get_audio_duration_seconds(audio_path)
        enable_chunking = chunking if chunking is not None else os.getenv("V1_ASR_CHUNKING", "true").lower() in ("true", "1", "yes")

        short_thresh = float(os.getenv("V1_SHORT_MAX_SECONDS", "420.0"))
        if enable_chunking and total_dur > (chunk_size_s if chunking is not None else short_thresh):
            import tempfile
            logging.getLogger(__name__).info(
                f"[ASR_CHUNKING] Audio dài {total_dur:.1f}s (>{short_thresh:.0f}s ~ 7 phút) -> Áp dụng Safe Chunking (240s, 0.75s overlap)..."
            )
            intervals = chunk_audio_intervals(total_dur, chunk_size=chunk_size_s, overlap=overlap_s)
            transcribed_segments = []
            with tempfile.TemporaryDirectory() as tmpdir:
                for idx, (c_start, c_end) in enumerate(intervals):
                    chunk_wav = os.path.join(tmpdir, f"chunk_{idx:03d}.wav")
                    slice_audio_chunk(audio_path, c_start, c_end - c_start, chunk_wav)
                    chunk_segs = _transcribe_raw_segments(model, chunk_wav)
                    for s in chunk_segs:
                        s["start"] = round(s["start"] + c_start, 3)
                        s["end"] = round(s["end"] + c_start, 3)
                    chunk_segs = deduplicate_boundary_segments(transcribed_segments, chunk_segs, c_start)
                    transcribed_segments.extend(chunk_segs)
        else:
            transcribed_segments = _transcribe_raw_segments(model, audio_path)

        merged_fast = _merge_short_fragments(transcribed_segments)
        
        # Tự động phát hiện original.wav nếu chưa truyền vào
        if original_audio_path is None:
            p = Path(audio_path)
            for cand in [
                p.parent / "original.wav",
                p.parents[1] / "original.wav" if len(p.parents) > 1 else None,
                p.parents[2] / "original.wav" if len(p.parents) > 2 else None,
            ]:
                if cand and cand.is_file():
                    original_audio_path = str(cand)
                    break

        if original_audio_path and os.path.exists(original_audio_path):
            try:
                try:
                    from .v1_conditional_asr import run_conditional_asr
                except ImportError:
                    from ai.v1_conditional_asr import run_conditional_asr
                subs = [
                    srt.Subtitle(
                        index=i,
                        start=timedelta(seconds=seg["start"]),
                        end=timedelta(seconds=seg["end"]),
                        content=seg["text"].strip(),
                    )
                    for i, seg in enumerate(merged_fast, start=1)
                ]
                refined = run_conditional_asr(
                    original_audio_path=original_audio_path,
                    initial_segments=subs,
                    whisper_model=model,
                )
                return [
                    {"start": s.start.total_seconds(), "end": s.end.total_seconds(), "text": s.content}
                    for s in refined
                ]
            except Exception as cond_err:
                logging.getLogger(__name__).warning("Conditional ASR fallback skipped: %s", cond_err)

        return merged_fast
    finally:
        if model is not None:
            del model
        gc.collect()
        if torch.cuda.is_available():
            torch.cuda.empty_cache()
        print("🧹 Đã giải phóng bộ nhớ RAM/VRAM của Whisper AI.")


def extract_subtitles_whisper(audio_path, output_srt_path, num_workers=2, original_audio_path=None, asr_model=None, *, model_id=None, chunking=None, chunk_size_s=240.0, overlap_s=0.75):
    """Transcribe with V1's fast model (or Qwen3-ASR if selected), falling back to the proven model."""

    try:
        from .v1_qwen_asr_adapter import is_qwen_asr_enabled, transcribe_audio_qwen, check_qwen_readiness
    except ImportError:
        from ai.v1_qwen_asr_adapter import is_qwen_asr_enabled, transcribe_audio_qwen, check_qwen_readiness

    aliases = {"whisper": "whisper_turbo", "whisper_large_v3_turbo": "whisper_turbo", "qwen": "qwen3_asr", "qwen3": "qwen3_asr", "qwen3_asr_preview": "qwen3_asr"}
    selected = model_id if model_id is not None else asr_model
    model_id = aliases.get(str(selected).lower(), selected) if selected is not None else None
    if model_id not in (None, "whisper_turbo", "qwen3_asr"):
        raise ValueError(f"Unsupported ASR model: {model_id}")
    use_qwen = model_id == "qwen3_asr" if model_id is not None else is_qwen_asr_enabled()
    if use_qwen:
        ready, reason = check_qwen_readiness(ignore_flag=True)
        if ready:
            print("🚀 [ASR] Người dùng đã chọn Qwen3-ASR 0.6B GPU. Đang thực thi nhận dạng...")
            try:
                if chunking:
                    from .v1_asr_chunking import get_audio_duration_seconds, chunk_audio_intervals, slice_audio_chunk, deduplicate_boundary_segments
                    import tempfile
                    merged = []
                    intervals = chunk_audio_intervals(get_audio_duration_seconds(audio_path), chunk_size_s, overlap_s)
                    with tempfile.TemporaryDirectory(prefix="v1-qwen-chunks-") as folder:
                        for number, (start, end) in enumerate(intervals):
                            chunk_path = str(Path(folder) / f"{number}.wav")
                            slice_audio_chunk(audio_path, start, end - start, chunk_path)
                            chunk_subs = transcribe_audio_qwen(chunk_path, None)
                            rows = [{"start": s.start.total_seconds() + start,
                                     "end": s.end.total_seconds() + start, "text": s.content}
                                    for s in chunk_subs]
                            merged.extend(deduplicate_boundary_segments(merged, rows, start))
                    qwen_subs = [srt.Subtitle(i, timedelta(seconds=row["start"]),
                                 timedelta(seconds=row["end"]), row["text"])
                                 for i, row in enumerate(merged, 1)]
                    save_srt(qwen_subs, output_srt_path)
                else:
                    qwen_subs = transcribe_audio_qwen(audio_path, output_srt_path)
                if qwen_subs:
                    print(f"✅ Qwen3-ASR hoàn tất nhận dạng thành công ({len(qwen_subs)} câu thoại).")
                    # Do not set custom attributes on srt.Subtitle to avoid TypeError in Subtitle.__init__
                    pass
                    return qwen_subs
            except Exception as qwen_err:
                print(f"⚠️ Qwen3-ASR gặp lỗi: {qwen_err}. Tự động fallback sang Faster-Whisper Large-v3 Turbo...")
        else:
            print(f"⚠️ Qwen3-ASR chưa sẵn sàng ({reason}). Sử dụng Faster-Whisper mặc định.")

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
                original_audio_path=original_audio_path,
                chunking=chunking, chunk_size_s=chunk_size_s, overlap_s=overlap_s,
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
        output_file.write(srt.compose(srt_segments, reindex=False))

    return srt_segments

def save_srt(srt_segments, output_path):
    with open(output_path, "w", encoding="utf-8") as f:
        f.write(srt.compose(srt_segments, reindex=False))
