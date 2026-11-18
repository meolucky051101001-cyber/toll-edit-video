"""Bounded second reading for conflicting ASR and source captions.

Audio clips retain original speed. Corrections require independent caption
samples and are constrained by visual_caption_repair; no cue is added or timed.
"""
from __future__ import annotations
from difflib import SequenceMatcher
import hashlib
import json
from pathlib import Path
import re
import subprocess
import tempfile
import time


def validate_review_proposal(segment, proposal, sampled_times, *, audio_sha256):
    try:
        from visual_caption_repair import validate_visual_caption_correction, derive_caption_substitution
    except ImportError:
        from .visual_caption_repair import validate_visual_caption_correction, derive_caption_substitution
    direct = validate_visual_caption_correction(segment, proposal, sampled_times=sampled_times,
        allow_audio_corroboration=True, audio_sha256=audio_sha256)
    if direct["accepted"]:
        return direct
    derived = derive_caption_substitution(segment, proposal, sampled_times=sampled_times)
    return derived if derived is not None and derived["accepted"] else direct


def review_caption_conflicts(input_video, segments, gemini_key, audit_path):
    import cv2
    from google import genai
    from google.genai import types

    def cjk(text):
        return "".join(re.findall(r"[\u4e00-\u9fff]", str(text)))

    pending = []
    for segment in segments:
        if segment.get("ocr_correction"):
            continue
        caption = segment.get("best_block") or {}
        source_text, caption_text = cjk(segment["text"]), cjk(caption.get("text"))
        if caption and source_text != caption_text and source_text not in caption_text and caption_text not in source_text:
            pending.append(segment)
    if not pending:
        Path(audit_path).write_text(json.dumps({"reviewed": [], "correction_count": 0}), encoding="utf-8")
        return segments

    import os

    def _split_keys(raw_s: str) -> list[str]:
        out: list[str] = []
        for p in re.split(r"[\s,;]+", str(raw_s or "")):
            ck = p.strip().strip('"').strip("'")
            if ck and ck not in out:
                out.append(ck)
        return out

    primary_list = _split_keys(gemini_key) or _split_keys(os.getenv("GEMINI_API_KEY", ""))
    pool_list = _split_keys(os.getenv("GEMINI_API_KEYS", ""))
    api_keys = list(primary_list)
    if pool_list and (not gemini_key or any(k in pool_list for k in primary_list)):
        for pk in pool_list:
            if pk not in api_keys:
                api_keys.append(pk)
    if not api_keys:
        api_keys = [gemini_key]
    http_opts_cls = getattr(types, "HttpOptions", None)
    retry_cls = getattr(types, "HttpRetryOptions", None)

    def _build_client(key_val: str):
        if callable(http_opts_cls):
            opts_kw = {"timeout": 120000}
            if callable(retry_cls):
                try:
                    opts_kw["retry_options"] = retry_cls(attempts=1)
                except TypeError:
                    pass
            try:
                return genai.Client(api_key=key_val, http_options=http_opts_cls(**opts_kw))
            except TypeError:
                try:
                    return genai.Client(api_key=key_val, http_options=http_opts_cls(timeout=120000))
                except TypeError:
                    pass
        return genai.Client(api_key=key_val)

    clients = [_build_client(k) for k in api_keys]
    unavailable_models = set()
    cache_dir = Path(audit_path).parent / "caption_review_cache"
    cache_dir.mkdir(exist_ok=True)
    call_counter = [0]
    def review(segment):
        samples = [row for row in segment.get("ocr_caption_rows", []) if len(cjk(row.get("text"))) >= 3]
        center = (segment["start"] + segment["end"]) / 2
        if samples:
            best = max(samples, key=lambda row: SequenceMatcher(None, cjk(segment["text"]), cjk(row.get("text"))).ratio())
            center = float(best["sample_time"])
        spread = min(.12, (segment["end"] - segment["start"]) / 3)
        center = max(segment["start"] + spread, min(segment["end"] - spread, center))
        times = [round(center + offset, 3) for offset in (-spread, 0, spread)]
        prompt = (
            "Independently transcribe the audible Chinese speech in the supplied original-speed audio into audio_transcript. "
            "Read each caption image literally into caption_reads with its supplied sample_time; do not copy a prior image's text. "
            "Then propose corrected_text for the supplied ASR cue only if audio and visible captions support a homophone correction. "
            "Preserve every spoken clause, repetition, number, name and action. Do not substitute scene text for speech or invent a phrase. "
            "The caption may extend beyond this cue: preserve only this cue's spoken text. Do not add/delete words to fit the caption. "
            "If unsure, keep the original text. Return JSON object {id, audio_transcript, caption_reads:[{sample_time,text}], corrected_text}.\n"
            + json.dumps({"id": segment["id"], "asr_text": segment["text"], "start": segment["start"], "end": segment["end"]}, ensure_ascii=False)
        )
        content = [prompt]
        frame_evidence = []
        cap = cv2.VideoCapture(str(input_video))
        try:
            for time_s in times:
                cap.set(cv2.CAP_PROP_POS_MSEC, time_s * 1000)
                ok, frame = cap.read()
                if not ok:
                    raise RuntimeError(f"Cannot read caption frame for cue {segment['id']}")
                height, width = frame.shape[:2]
                box = segment.get("best_block") or {}
                top = max(0, int((float(box.get("y_pct", .7)) - .045) * height))
                bottom = min(height, int((float(box.get("max_y_pct", .86)) + .045) * height))
                crop = frame[top:bottom, :]
                ok, encoded = cv2.imencode(".jpg", crop)
                if not ok:
                    raise RuntimeError("Cannot encode caption crop")
                image_bytes = encoded.tobytes()
                frame_evidence.append({"sample_time": time_s, "sha256": hashlib.sha256(image_bytes).hexdigest()})
                content += [f"Caption at sample_time={time_s}:", types.Part.from_bytes(data=image_bytes, mime_type="image/jpeg")]
        finally:
            cap.release()
        with tempfile.TemporaryDirectory(prefix="caption-review-", dir=str(Path(audit_path).parent)) as temp:
            audio_path = Path(temp) / "source.wav"
            result = subprocess.run(["ffmpeg", "-v", "error", "-y", "-ss", str(segment["start"]),
                "-i", str(input_video), "-t", str(segment["end"] - segment["start"]), "-vn", "-ac", "1", "-ar", "16000", str(audio_path)],
                capture_output=True, timeout=45)
            if result.returncode:
                raise RuntimeError(f"Cannot extract source audio cue {segment['id']}")
            audio = audio_path.read_bytes()
        content += ["Original audio for this cue:", types.Part.from_bytes(data=audio, mime_type="audio/wav")]
        identity = {"id": segment["id"], "text": segment["text"], "frames": frame_evidence,
                    "audio_sha256": hashlib.sha256(audio).hexdigest(), "policy": 1}
        cache_file = cache_dir / (hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest() + ".json")
        if cache_file.is_file():
            cached = json.loads(cache_file.read_text(encoding="utf-8"))
            cached["validation"] = validate_review_proposal(segment, cached["proposal"], times,
                audio_sha256=identity["audio_sha256"])
            return cached
        error = "No caption-review model available after prior quota or availability failures"
        for model in ("gemini-3.1-flash-lite-preview", "gemini-3.5-flash-lite", "gemini-3-flash-preview", "gemini-flash-lite-latest"):
            for attempt in range(max(2, len(clients))):
                if model in unavailable_models:
                    break
                active_client = clients[call_counter[0] % len(clients)]
                call_counter[0] += 1
                try:
                    response = active_client.models.generate_content(model=model, contents=content,
                        config=types.GenerateContentConfig(response_mime_type="application/json", temperature=0.0))
                    proposal = json.loads(response.text)
                    verdict = validate_review_proposal(segment, proposal, times,
                        audio_sha256=identity["audio_sha256"])
                    report = {"id": segment["id"], "original_text": segment["text"], "proposal": proposal,
                            "validation": verdict, "frames": frame_evidence, "model": model,
                            "audio_sha256": identity["audio_sha256"], "audio_interval": [segment["start"], segment["end"]]}
                    cache_file.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
                    return report
                except Exception as exc:
                    error = str(exc)
                    for k_sec in api_keys:
                        if k_sec:
                            error = error.replace(k_sec, "[REDACTED]")
                    print(f"Caption review cue {segment['id']} ({model}) attempt {attempt+1}: {error[:300]}", flush=True)
                    if "404" in error or ("429" in error and attempt + 1 >= len(clients)):
                        unavailable_models.add(model)
                        break
                    if attempt == 0:
                        time.sleep(1.5)
        return {"id": segment["id"], "error": error, "original_text": segment["text"]}

    # Keep provider calls serial: parallel requests can exhaust a model's
    # per-minute quota before the shared unavailable-model state is updated.
    reports = [review(segment) for segment in pending]
    errors = [report["id"] for report in reports if report.get("error")]
    Path(audit_path).write_text(json.dumps({"reviewed": reports, "failed_cue_ids": errors,
        "correction_count": sum(bool(report.get("validation", {}).get("accepted")) for report in reports)}, ensure_ascii=False, indent=2), encoding="utf-8")
    if errors:
        raise RuntimeError(f"Caption/audio verification failed after bounded retries for cue(s) {errors}")
    by_id = {segment["id"]: segment for segment in segments}
    for report in reports:
        if report["validation"]["accepted"]:
            segment = by_id[report["id"]]
            segment["text"] = report["validation"]["text"]
            segment["ocr_correction"] = {"method": "caption_audio_corroboration", "asr_text": report["original_text"],
                                         "audit": report["validation"]["audit"]}
    Path(audit_path).write_text(json.dumps({"reviewed": reports,
        "correction_count": sum(bool(report["validation"]["accepted"]) for report in reports)}, ensure_ascii=False, indent=2), encoding="utf-8")
    return segments
