"""
full_pipeline.py - Pipeline lồng tiếng tự động 8 bước chuẩn V1 chạy TRỰC TIẾP trên Colab VM.
Tận dụng GPU T4/L4 để tăng tốc:
  1. Trích xuất âm thanh từ video.
  2. Tách vocal và nhạc nền sạch (Demucs htdemucs GPU).
  3. ASR Whisper Large-v3 trên vocal track.
  4. Dịch phụ đề tự nhiên (Gemini 2.0 Flash AI).
  5. TTS CapCut Mai (BV562_streaming) / Edge TTS fallback.
  6. Hòa âm Dynamic Ducking EBU R128 (FFmpeg sidechain + loudnorm).
  7. Render video thành phẩm (FFmpeg libx264 + aac).
  8. Xuất metadata & nghiệm thu.
"""

import argparse
import asyncio
from datetime import timedelta
from difflib import SequenceMatcher
import json
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


def log_step(step_num: int, total: int, message: str):
    print(f"\n[{step_num}/{total}] {message}", flush=True)


def run_cmd(cmd: list[str], desc: str):
    print(f"  -> Chạy: {' '.join(cmd[:6])}...", flush=True)
    res = subprocess.run(cmd, capture_output=True, text=True)
    if res.returncode != 0:
        print(f"  [ERROR] {desc} thất bại:\n{res.stderr[-600:]}", file=sys.stderr, flush=True)
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


def step3_asr_whisper(audio_path: str, output_srt: str) -> list[dict]:
    log_step(3, 8, "Nhận dạng giọng nói (ASR) bằng Whisper Large-v3...")
    import torch
    import whisper

    device = "cuda" if torch.cuda.is_available() else "cpu"
    print(f"  -> Whisper sử dụng thiết bị: {device}", flush=True)
    model = whisper.load_model("large-v3", device=device)
    result = model.transcribe(audio_path, verbose=False)

    segments = []
    for idx, s in enumerate(result.get("segments", []), 1):
        segments.append({
            "id": idx,
            "start": round(s["start"], 3),
            "end": round(s["end"], 3),
            "text": s["text"].strip(),
        })

    # Ghi file SRT gốc
    with open(output_srt, "w", encoding="utf-8") as f:
        for idx, seg in enumerate(segments, 1):
            start_ts = format_timestamp(seg["start"])
            end_ts = format_timestamp(seg["end"])
            f.write(f"{idx}\n{start_ts} --> {end_ts}\n{seg['text']}\n\n")

    print(f"  -> Đã nhận dạng được {len(segments)} câu thoại.", flush=True)
    return segments


def _cjk_only_cloud(text: str) -> str:
    return "".join(re.findall(r"[\u4e00-\u9fff\u3400-\u4dbf]", str(text or "")))


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
    it = iter(asr_cjk)
    if len(ocr_cjk) <= len(asr_cjk) and all(ch in it for ch in ocr_cjk):
        return False
    sim = SequenceMatcher(None, asr_cjk, ocr_cjk).ratio()
    if sim < 0.99:
        print(f"  [OCR Fix] '{asr_text}' -> '{combined_ocr}' (sim={sim:.2f}, prob={avg_prob:.2f})", flush=True)
        seg["text"] = combined_ocr
        return True
    return False


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
    video_duration = max((float(s["end"]) for s in segments), default=0.0)
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
        if fps > 0 and frame_count > 0:
            video_duration = max(video_duration, frame_count / fps)

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
                sample_times = [st + dur * 0.25, st + dur * 0.75]
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
                        in_subtitle_band=False,
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


def step4_translate_subtitles(segments: list[dict], gemini_key: str, vi_srt_path: str = "subtitles_vi.srt") -> list[dict]:
    log_step(4, 8, "Dịch phụ đề sang tiếng Việt tự nhiên (Gemini AI)...")
    if not segments:
        return segments

    # Chuẩn bị key: Ưu tiên gemini_key truyền vào, fallback sang biến môi trường
    actual_key = gemini_key or os.getenv("GEMINI_API_KEY", "")
    if not actual_key:
        print("  [WARN] Không tìm thấy GEMINI_API_KEY -> Giữ nguyên câu gốc.", flush=True)
        for s in segments:
            s["translated_text"] = s["text"]
        return segments

    candidate_models = ["gemini-3-flash-preview", "gemini-3.5-flash-lite", "gemini-3.6-flash"]
    translated_success = False

    try:
        from google import genai
        from google.genai import types

        client = genai.Client(api_key=actual_key)
        input_texts = [{"id": s["id"], "text": s["text"]} for s in segments]
        prompt = (
            "Bạn là chuyên gia dịch thuật và lồng tiếng video chuyên nghiệp. "
            "Dịch các câu thoại sau từ tiếng Trung sang tiếng Việt chuẩn, tự nhiên, văn phong đời thực, súc tích để đọc lồng tiếng. "
            "BẮT BUỘC giữ nguyên định dạng JSON list: [{\"id\": ..., \"text\": \"...\"}]:\n"
            + json.dumps(input_texts, ensure_ascii=False)
        )

        for model_name in candidate_models:
            try:
                response = client.models.generate_content(
                    model=model_name,
                    contents=prompt,
                    config=types.GenerateContentConfig(response_mime_type="application/json")
                )
                raw_json = response.text or "[]"
                translated_data = json.loads(raw_json)
                trans_map = {item["id"]: item["text"] for item in translated_data if "id" in item and "text" in item}

                if len(trans_map) > 0:
                    for s in segments:
                        s["translated_text"] = trans_map.get(s["id"], s["text"])
                    print(f"  -> Đã dịch thành công {len(segments)} câu thoại bằng {model_name}.", flush=True)
                    translated_success = True
                    break
            except Exception as m_err:
                print(f"  [WARN] Model {model_name} không khả dụng ({m_err}), thử model tiếp theo...", flush=True)

    except Exception as exc:
        print(f"  [WARN] Lỗi khởi tạo Gemini Client ({exc}).", flush=True)

    if not translated_success:
        print("  [WARN] Tất cả model Gemini gặp sự cố, giữ nguyên text.", flush=True)
        for s in segments:
            if "translated_text" not in s:
                s["translated_text"] = s["text"]

    # Ghi file phụ đề tiếng Việt (subtitles_vi.srt) phục vụ lồng phụ đề (Hardsub)
    try:
        with open(vi_srt_path, "w", encoding="utf-8") as f:
            for idx, seg in enumerate(segments, 1):
                start_ts = format_timestamp(seg["start"])
                end_ts = format_timestamp(seg["end"])
                text_vi = seg.get("translated_text", seg["text"])
                f.write(f"{idx}\n{start_ts} --> {end_ts}\n{text_vi}\n\n")
        print(f"  -> Đã ghi file phụ đề tiếng Việt: {vi_srt_path}", flush=True)
    except Exception as e:
        print(f"  [WARN] Không thể ghi file {vi_srt_path}: {e}", flush=True)

    return segments


def generate_capcut_tts_single(text: str, voice: str, out_file: str) -> bool:
    """Gọi CapCut TTS SDK trực tiếp với timeout ngắn tối ưu (4s)."""
    try:
        from capcut_tts_api import CapCutClient
        client = CapCutClient()
        res = client.create_tts_task(texts=text, voice=voice)
        tasks = res.get("data", {}).get("tasks", [])
        if not tasks:
            return False
        task_id = tasks[0]["id"]
        token = tasks[0]["token"]

        deadline = time.monotonic() + 4.0
        while time.monotonic() < deadline:
            time.sleep(0.4)
            query_res = client.query_tts_task(task_id, token)
            q_tasks = query_res.get("data", {}).get("tasks", [])
            if not q_tasks:
                break
            status = q_tasks[0].get("status")
            if status in ("success", "succeed"):
                payload = json.loads(q_tasks[0].get("payload", "{}"))
                speech_url = payload.get("audio_subtitles", [{}])[0].get("speech_url")
                if speech_url:
                    r = client.session.get(speech_url, timeout=10)
                    r.raise_for_status()
                    if len(r.content) >= 128:
                        with open(out_file, "wb") as f:
                            f.write(r.content)
                        return True
            elif status == "failed":
                return False
        return False
    except Exception as exc:
        print(f"    [CapCut TTS Warn] {exc}", flush=True)
        return False


async def generate_single_tts(text: str, voice: str, out_file: str):
    import edge_tts
    communicate = edge_tts.Communicate(text, voice)
    await communicate.save(out_file)


def step5_generate_tts(segments: list[dict], voice: str, tts_dir: str):
    is_capcut = voice.startswith("BV") or "capcut" in voice.lower()
    capcut_voice = voice.replace("capcut-", "") if "capcut-" in voice else voice
    if is_capcut and not capcut_voice.startswith("BV"):
        capcut_voice = "BV562_streaming"

    edge_fallback = "vi-VN-NamMinhNeural" if ("075" in voice or "male" in voice.lower()) else "vi-VN-HoaiMyNeural"
    engine_name = f"CapCut TTS ({capcut_voice})" if is_capcut else f"Edge TTS ({voice})"

    log_step(5, 8, f"Tổng hợp giọng đọc lồng tiếng ({engine_name})...")
    os.makedirs(tts_dir, exist_ok=True)

    async def _run_all():
        capcut_available = is_capcut
        capcut_consecutive_fails = 0

        for s in segments:
            t_text = s.get("translated_text", s["text"]).strip()
            if not t_text:
                continue
            out_path = os.path.join(tts_dir, f"seg_{s['id']:04d}.mp3")
            s["tts_path"] = out_path

            success = False
            if capcut_available:
                success = await asyncio.to_thread(generate_capcut_tts_single, t_text, capcut_voice, out_path)
                if success:
                    capcut_consecutive_fails = 0
                else:
                    capcut_consecutive_fails += 1
                    print(f"  [WARN] Đoạn {s['id']} CapCut TTS lỗi (lần {capcut_consecutive_fails}) -> Chuyển fallback Edge TTS...", flush=True)
                    if capcut_consecutive_fails >= 2:
                        capcut_available = False
                        print("  [INFO] Phát hiện CapCut API không khả dụng trên Cloud -> Tự động chuyển toàn bộ sang Edge TTS siêu tốc để tối ưu thời gian xử lý.", flush=True)

            if not success:
                try:
                    await generate_single_tts(t_text, edge_fallback, out_path)
                except Exception as e:
                    print(f"  [WARN] Lỗi Edge TTS đoạn {s['id']}: {e}", flush=True)

    asyncio.run(_run_all())

    # Nén tốc độ FFmpeg atempo chống đè thoại kế tiếp chuẩn V1
    try:
        from v1_timing_solver import calculate_timing_windows, calculate_atempo_factor, build_safe_atempo_filter

        def _probe_audio_dur(path: str) -> float:
            try:
                out = subprocess.check_output(
                    ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "default=noprint_wrappers=1:nokey=1", path],
                    text=True,
                ).strip()
                if out:
                    return float(out)
            except Exception:
                pass
            from pydub import AudioSegment
            return len(AudioSegment.from_file(path)) / 1000.0

        seg_stubs = [
            SimpleNamespace(index=s["id"], start=timedelta(seconds=float(s["start"])), end=timedelta(seconds=float(s["end"])))
            for s in segments
        ]
        windows = calculate_timing_windows(seg_stubs, hard_gap_s=0.08)
        adjusted = 0
        for idx, s in enumerate(segments):
            tts_p = s.get("tts_path")
            if not tts_p or not os.path.exists(tts_p) or os.path.getsize(tts_p) <= 0:
                continue
            raw_dur = _probe_audio_dur(tts_p)
            win_dur = float(windows.get(s["id"], windows.get(idx, max(0.25, float(s["end"]) - float(s["start"])))))
            speed, status = calculate_atempo_factor(raw_dur, win_dur, min_speed=1.0, max_speed=1.5)
            if speed > 1.01:
                af = build_safe_atempo_filter(speed)
                if af:
                    tmp_fit = tts_p + ".fit.mp3"
                    fit_cmd = ["ffmpeg", "-y", "-i", tts_p, "-filter:a", af, "-c:a", "libmp3lame", "-b:a", "192k", tmp_fit]
                    fit_res = subprocess.run(fit_cmd, capture_output=True, text=True)
                    if fit_res.returncode == 0 and os.path.exists(tmp_fit) and os.path.getsize(tmp_fit) > 128:
                        os.replace(tmp_fit, tts_p)
                        raw_dur = _probe_audio_dur(tts_p)
                        adjusted += 1
                    else:
                        if os.path.exists(tmp_fit):
                            os.remove(tmp_fit)
            s["tts_duration"] = raw_dur
        print(f"  -> Đã cân chỉnh chống đè thoại (atempo) cho {adjusted}/{len(segments)} câu.", flush=True)
    except Exception as exc:
        print(f"  [WARN] Bỏ qua cân chỉnh atempo ({exc}).", flush=True)

    print("  -> Tạo file giọng đọc hoàn tất.", flush=True)


def step6_mix_audio_ducking_ebu_r128(bgm_wav: str, segments: list[dict], output_final_wav: str):
    """
    Hòa âm Dynamic Ducking chuẩn EBU R128:
    - Nhạc nền (BGM sạch từ Demucs) tự động nén xuống khi có lời đọc (sidechain compression).
    - Giọng đọc rõ ràng, giữ nhịp tự nhiên.
    - Chuẩn hóa âm lượng EBU R128 (-15.0 LUFS, True Peak -1.0 dBTP).
    """
    log_step(6, 8, "Hòa âm Dynamic Ducking & Chuẩn hóa EBU R128 (Studio Quality)...")

    dubbing_audio_files = []
    for s in segments:
        tts_file = s.get("tts_path")
        if tts_file and os.path.exists(tts_file) and os.path.getsize(tts_file) > 0:
            dubbing_audio_files.append({
                "path": tts_file,
                "start": float(s["start"]),
            })

    if not dubbing_audio_files:
        print("  -> Không có đoạn lồng tiếng nào. Xuất thẳng nhạc nền.", flush=True)
        shutil.copy2(bgm_wav, output_final_wav)
        return

    # Thử hòa âm bằng module mixer chuẩn Tool V1 (FFmpeg native filter graph)
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
        return
    except Exception as exc:
        print(f"  [WARN] Lỗi FFmpeg mixer ({exc}). Tự động fallback sang Pydub overlay an toàn...", flush=True)

    # Fallback an toàn bằng Pydub nếu FFmpeg native gặp sự cố
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


def build_ass_subtitles_v1(
    segments: list[dict],
    ass_path: str,
    video_w: int = 1080,
    video_h: int = 1920,
    main_y_pct: float = 0.78,
    video_duration: float | None = None,
) -> bool:
    try:
        import srt
        from ass_utils import sync_and_clamp_subtitles, generate_ass_file

        srt_segs = []
        dub_files = []
        for s in segments:
            seg = srt.Subtitle(
                index=int(s["id"]),
                start=timedelta(seconds=float(s["start"])),
                end=timedelta(seconds=float(s["end"])),
                content=str(s.get("translated_text", s["text"]) or "").strip(),
            )
            bb = s.get("best_block")
            if isinstance(bb, dict):
                seg.best_block = SimpleNamespace(
                    **bb,
                    is_subtitle=True,
                    in_subtitle_band=True,
                    is_packaging=False,
                    type="subtitle",
                )
            else:
                seg.best_block = bb
            seg.tracking_blocks = [
                SimpleNamespace(**tb, is_subtitle=True, in_subtitle_band=True, is_packaging=False, type="subtitle")
                for tb in (s.get("tracking_blocks") or [])
                if isinstance(tb, dict)
            ]
            seg.y_pct = float(s.get("y_pct", main_y_pct))
            seg.max_y_pct = float(s.get("max_y_pct", min(0.95, main_y_pct + 0.06)))
            seg.ocr_mask_status = s.get("ocr_mask_status", "no_caption_evidence")
            seg.is_subtitle = True
            seg.in_subtitle_band = bool(seg.best_block)
            seg.is_packaging = False
            srt_segs.append(seg)

            tts_p = s.get("tts_path")
            if tts_p and os.path.exists(tts_p):
                dub_files.append({
                    "index": int(s["id"]),
                    "path": tts_p,
                    "start": float(s["start"]),
                    "duration": float(s.get("tts_duration", 0.0)),
                })

        clamped = sync_and_clamp_subtitles(srt_segs, dubbing_audio_files=dub_files)
        generate_ass_file(
            clamped,
            [],
            ass_path,
            play_res_x=video_w,
            play_res_y=video_h,
            main_y_pct=main_y_pct,
            video_duration=video_duration,
            strip_terminal_punctuation=True,
        )
        ok = os.path.exists(ass_path) and os.path.getsize(ass_path) > 100
        if ok:
            print(f"  -> Đã tạo file phụ đề & hộp che bo góc chuẩn V1: {ass_path}", flush=True)
        return ok
    except Exception as exc:
        print(f"  [WARN] Lỗi tạo file .ass chuẩn V1 ({exc}).", flush=True)
        return False


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
    run_cmd(cpu_cmd, "Render video bằng CPU libx264")
    print(f"  -> Render video bằng CPU hoàn tất: {output_mp4}", flush=True)


def step0_download_video_from_url(url: str, output_path: str = "downloaded_video.mp4") -> str:
    """
    Tải video trực tiếp từ link (TikTok, Douyin, YouTube, Facebook, Kuaishou, link trực tiếp...)
    sử dụng yt-dlp trực tiếp trên Colab VM với tốc độ mạng datacenter 1Gbps.
    """
    log_step(0, 8, f"Tải video từ liên kết trên Colab VM: {url}...")
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
        print(f"  [WARN] yt-dlp gặp sự cố ({exc}). Thử tải trực tiếp stream qua requests...", flush=True)
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
    parser.add_argument("--gemini-key", default=os.getenv("GEMINI_API_KEY", ""), help="Gemini API Key")
    args = parser.parse_args()

    start_time = time.time()
    input_target = args.input

    print(f"=== BẮT ĐẦU PIPELINE CLOUD 8 BƯỚC CHUẨN V1 TRÊN COLAB VM ===")
    print(f"Mục tiêu đầu vào: {input_target}")
    print(f"Giọng đọc: {args.voice}")

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

        # Bước 3: ASR Whisper Large-v3 trên vocal track
        segments = step3_asr_whisper(vocals_path, output_srt)

        # Bước 3b: Quét OCR GPU định vị hộp che phụ đề & sửa lỗi đồng âm
        segments, main_y_pct, video_w, video_h, video_duration = step3b_ocr_and_fix_subtitles(
            input_video, segments, output_srt
        )

        # Bước 4: Dịch phụ đề tự nhiên (Gemini AI)
        segments = step4_translate_subtitles(segments, args.gemini_key)

        # Bước 5: Tổng hợp giọng đọc TTS (CapCut Mai / Edge) + Cân chỉnh atempo chống đè thoại
        step5_generate_tts(segments, args.voice, tts_dir)

        # Bước 6: Hòa âm Dynamic Sidechain Ducking EBU R128
        step6_mix_audio_ducking_ebu_r128(bgm_path, segments, final_audio)

        # Tạo file phụ đề & hộp che bo góc động (.ass) chuẩn Tool V1
        has_ass = build_ass_subtitles_v1(
            segments,
            output_ass,
            video_w=video_w,
            video_h=video_h,
            main_y_pct=main_y_pct,
            video_duration=video_duration,
        )

        # Bước 7: Render video thành phẩm
        step7_render_video(
            input_video,
            final_audio,
            output_video,
            ass_path=output_ass if has_ass else "",
        )

        # Bước 8: Xuất metadata và hoàn tất
        elapsed = round(time.time() - start_time, 2)
        log_step(8, 8, f"Hoàn tất trọn vẹn trong {elapsed} giây!")

        meta = {
            "status": "completed",
            "elapsed_seconds": elapsed,
            "segments_count": len(segments),
            "voice": args.voice,
            "vocal_separation": "demucs_htdemucs",
            "mixing": "ebu_r128_sidechain_ducking",
            "ocr_subtitle_band": round(main_y_pct, 4),
            "ass_subtitles": has_ass,
            "output_video": output_video,
        }
        with open(metadata_file, "w", encoding="utf-8") as f:
            json.dump(meta, f, indent=2, ensure_ascii=False)

        print("\n[SUCCESS] Pipeline Colab Cloud V1 hoàn tất 100%!")

    except Exception as exc:
        print(f"\n[FATAL ERROR] Pipeline Cloud thất bại: {exc}", file=sys.stderr)
        meta = {
            "status": "error",
            "error": str(exc),
            "elapsed_seconds": round(time.time() - start_time, 2),
        }
        with open(metadata_file, "w", encoding="utf-8") as f:
            json.dump(meta, f, indent=2, ensure_ascii=False)
        sys.exit(2)


if __name__ == "__main__":
    main()
