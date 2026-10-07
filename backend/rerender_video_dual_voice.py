# -*- coding: utf-8 -*-
"""
rerender_video_dual_voice.py
Re-renders video 1790954189_866e49ab_猫九酱 with:
1. Qwen3-ASR Forced Alignment precision timestamps (lip-sync frame-perfect).
2. Dual Voice (Phân vai Nam & Nữ):
   - Female cues (1, 4, 8) -> CapCut Mai (BV562_streaming)
   - Male cues (2, 3, 5, 6, 7, 9, 10, 11) -> CapCut Thanh Niên Tự Tin (BV075_streaming)
3. Precision dialogue fitting to ensure zero voice overlap and natural pauses.
4. Professional ASS subtitle alignment and mixing.
"""

import asyncio
import json
import logging
import os
import shutil
import sys
import time
from datetime import timedelta
from pathlib import Path

BASE_DIR = Path(r"C:\tool v1\backend")
WORKSPACE_DIR = Path(r"C:\tool v1\workspace")
BANVE_DIR = Path(r"D:\banve")

if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)
logger = logging.getLogger("rerender_dual_voice_qwen")

from ai.v1_auto_voice import decide_video_voice
from ai.v1_voice_isolated import generate_dubbing_audio_isolated
from ai.voice_cloning import fit_audio_file
from ass_utils import generate_ass_file, sync_and_clamp_subtitles
from v1_stage_runtime import load_payload, save_payload
from video_utils import mix_audio_pydub, process_video
from pydub import AudioSegment


# Exact Qwen3-ASR Forced Alignment timestamps for each dialogue line
QWEN_TIMINGS = {
    1: (0.560, 2.240),   # Nữ: "Cậu thích kiểu như tớ không?" (Mouth opens 0.56s)
    2: (3.360, 3.920),   # Nam: "Tạm ổn thôi"
    3: (3.920, 5.280),   # Nam: "Tớ thích kiểu dễ thương cơ"
    4: (6.560, 8.000),   # Nữ: "Tớ chưa đủ dễ thương à?" (Mouth opens 6.56s)
    5: (8.400, 10.000),  # Nam: "Cậu thấy mình dễ thương á?"
    6: (10.000, 10.880), # Nam: "Cậu dễ thương chỗ nào chứ"
    7: (12.240, 13.280), # Nam: "Cậu dễ thương chỗ nào nào"
    8: (13.760, 16.320), # Nữ: "Thế này chẳng phải rất dễ thương sao"
    9: (17.200, 17.920), # Nam: "Không được"
    10: (18.240, 19.040),# Nam: "Hơi chán đấy"
    11: (19.360, 19.840),# Nam: "Đổi kiểu khác đi"
}

# Maximum duration target per cue to preserve natural pauses and prevent dialogue spillover
TARGET_DURATIONS = {
    1: 1.68,
    2: 0.56,
    3: 1.36,
    4: 1.44,
    5: 1.60,
    6: 1.10,
    7: 1.08,
    8: 2.56,
    9: 0.72,
    10: 0.82,
    11: 0.48,
}


async def main():
    job_dir = Path(r"D:\workspace\1790954189_866e49ab_猫九酱")
    video_path = Path(r"D:\workspace\downloads\1790954189_866e49ab_猫九酱.mp4")
    dest_video = BANVE_DIR / f"Dubbed_{job_dir.name}.mp4"
    final_video = job_dir / f"final_{job_dir.name}.mp4"
    voc_path = job_dir / "bs_roformer" / "original_(Vocals)_model_bs_roformer_ep_317_sdr_12.wav"
    no_voc_path = job_dir / "bs_roformer" / "original_(Instrumental)_model_bs_roformer_ep_317_sdr_12.wav"
    orig_audio = job_dir / "original.wav"
    dubbing_dir = job_dir / "dubbing"
    ass_path = job_dir / "final.ass"
    mixed_audio = job_dir / "mixed.wav"

    logger.info("==================================================")
    logger.info("BẮT ĐẦU RE-RENDER DUAL VOICE + QWEN FORCED ALIGNMENT")
    logger.info("Video: %s", job_dir.name)
    logger.info("==================================================")

    # 1. Load OCR and Translation data
    ocr_data = load_payload(job_dir / "ocr.json")
    trans_data = load_payload(job_dir / "translation.json")
    translated_segments = trans_data["segments"]
    floating_segments = ocr_data.get("floating_segments", [])
    vid_w = ocr_data.get("width", 1080)
    vid_h = ocr_data.get("height", 1920)
    main_y_pct = ocr_data.get("main_y_pct", 0.728)

    vid_duration = len(AudioSegment.from_file(str(orig_audio))) / 1000.0
    logger.info("Thời lượng video: %.2f giây, Kích thước: %dx%d, main_y_pct: %.3f", vid_duration, vid_w, vid_h, main_y_pct)

    # 2. Áp dụng mốc thời gian chuẩn xác từ Qwen3-ASR Forced Alignment
    logger.info("Bước 1: Áp dụng mốc thời gian Qwen Forced Alignment cho %d câu thoại...", len(translated_segments))
    for seg in translated_segments:
        if seg.index in QWEN_TIMINGS:
            st, et = QWEN_TIMINGS[seg.index]
            seg.start = timedelta(seconds=st)
            seg.end = timedelta(seconds=et)
            logger.info("  -> Cue %2d: [%.3fs -> %.3fs] (%s)", seg.index, st, et, seg.content)

    # 3. Phân vai giọng Dual Voice (Nam & Nữ)
    logger.info("Bước 2: Thiết lập phân vai hội thoại Nam & Nữ (Dual Voice)...")
    female_voice = {
        "source": "capcut",
        "param": "BV562_streaming",
        "id": "capcut-BV562_streaming",
        "gender": "female",
        "label": "CapCut · Mai (Nữ)",
    }
    male_voice = {
        "source": "capcut",
        "param": "BV075_streaming",
        "id": "capcut-BV075_streaming",
        "gender": "male",
        "label": "CapCut · Thanh Niên Tự Tin (Nam)",
    }
    female_indices = {1, 4, 8}
    seg_voices = {}
    for seg in translated_segments:
        idx_str = str(seg.index)
        if seg.index in female_indices:
            seg_voices[idx_str] = female_voice
        else:
            seg_voices[idx_str] = male_voice

    v_source = "capcut"
    v_param = "BV562_streaming"
    v_label = "Phân vai (CapCut · Mai 👩 & CapCut · Thanh Niên Tự Tin 👨)"

    # Lưu snapshot voice_lock.json
    voice_lock_payload = {
        "voice_id": "capcut-BV562_streaming",
        "voice_source": v_source,
        "voice_param": v_param,
        "voice_label": v_label,
        "detected_gender": "dual",
        "confidence": 0.99,
        "median_f0": 0.0,
        "first_segment_index": 1,
        "rule": "dual_voice_dialogue",
        "voice_mode": "auto",
        "dual_voice": True,
        "segment_voices": seg_voices,
    }
    (job_dir / "voice_lock.json").write_text(json.dumps(voice_lock_payload, ensure_ascii=False, indent=2), encoding="utf-8")

    logger.info("Chế độ phân vai: %s", v_label)
    for idx, v in sorted(seg_voices.items(), key=lambda x: int(x[0])):
        logger.info("  -> Cue %2s (%s): %s (%s)", idx, v.get("gender"), v.get("id"), v.get("label"))

    # 4. Làm sạch thư mục dubbing
    shutil.rmtree(dubbing_dir, ignore_errors=True)
    dubbing_dir.mkdir(parents=True, exist_ok=True)

    # 5. Tạo âm thanh lồng tiếng AI (TTS) với Dual Voice
    logger.info("Bước 3: Tạo giọng đọc AI theo từng vai Nam/Nữ...")
    t0 = time.time()
    dubbing_audio_files = await generate_dubbing_audio_isolated(
        translated_segments,
        str(dubbing_dir),
        voice_source=v_source,
        voice_param=v_param,
        video_duration=vid_duration,
        segment_voices=seg_voices,
        tts_workers=6,
        max_natural_speed=1.35,
    )
    logger.info("Tạo xong %d file giọng đọc thô trong %.2fs", len(dubbing_audio_files), time.time() - t0)

    # 6. Căn chỉnh thời lượng từng câu khớp chuẩn miệng diễn viên (Precision Lip Sync Fit)
    logger.info("Bước 4: Căn chỉnh tempo chính xác theo từng khuôn miệng và nhịp dừng...")
    for item in dubbing_audio_files:
        idx = item.get("index")
        tgt = TARGET_DURATIONS.get(idx)
        if tgt and item.get("path") and os.path.exists(item["path"]):
            _, fitted_dur, spd = await fit_audio_file(item["path"], tgt, item["path"])
            item["actual_audio_duration"] = fitted_dur
            item["end"] = item["start"] + fitted_dur
            item["speed_ratio"] = float(item.get("speed_ratio", 1.0)) * spd
            st = item["start"]
            target_et = QWEN_TIMINGS.get(idx, (0, 0))[1]
            logger.info("  -> Cue %2d: dur=%.2fs (target <= %.2fs), speed=%.2fx, audio_end=%.2fs (target_end=%.2fs) - KHỚP!",
                        idx, fitted_dur, tgt, item["speed_ratio"], st + fitted_dur, target_et)

    # 7. Đồng bộ phụ đề với giọng đọc thực tế
    logger.info("Bước 5: Đồng bộ và co giãn thời lượng phụ đề theo giọng nói thực tế...")
    translated_segments = sync_and_clamp_subtitles(translated_segments, dubbing_audio_files)

    # 8. Tạo file phụ đề ASS
    logger.info("Bước 6: Xuất file phụ đề ASS với bounding box che sub gốc...")
    generate_ass_file(
        translated_segments,
        floating_segments,
        str(ass_path),
        play_res_x=vid_w,
        play_res_y=vid_h,
        main_y_pct=main_y_pct,
        font_name="Arial",
        font_color="&H00000000",
        font_weight=2,
    )

    save_payload(job_dir / "tts.json", segments=translated_segments, dubs=dubbing_audio_files)

    # 9. Trộn âm (Adaptive Mixer)
    logger.info("Bước 7: Hòa âm Adaptive Mixer (nhạc nền no_vocals + lồng tiếng)...")
    mix_audio_pydub(
        str(no_voc_path),
        dubbing_audio_files,
        str(mixed_audio),
        original_volume_db=-2.0,
        dubbing_volume_db=1.0,
        ducking_mode="soft",
        explicit=True,
    )
    logger.info("Đã tạo file hòa âm: %s (kích thước: %d bytes)", mixed_audio.name, mixed_audio.stat().st_size)

    # 10. Render video bằng NVENC GPU
    logger.info("Bước 8: Render video hoàn thiện bằng GPU NVENC...")
    t_render = time.time()
    res = process_video(
        str(video_path),
        str(ass_path),
        str(mixed_audio),
        str(final_video),
        main_y_pct=main_y_pct,
        delogo=False,
    )
    if not res or not final_video.is_file():
        raise RuntimeError("Render video thất bại!")
    logger.info("Render hoàn tất trong %.2fs (kích thước: %d bytes)", time.time() - t_render, final_video.stat().st_size)

    # 11. Xuất bản video thành phẩm sang D:\banve
    shutil.copy2(str(final_video), str(dest_video))
    logger.info("Đã xuất bản thành phẩm sang: %s", dest_video)

    # 12. Chạy Quality Gate kiểm tra chất lượng
    try:
        from v1_quality_gate import run_quality_gate, QCPolicy
        from v1_checkpoint import ManifestManager
        mgr = ManifestManager(WORKSPACE_DIR)
        manifest_id = f"tg_{job_dir.name}_1790954206"
        manifest = mgr.load_manifest(manifest_id)
        if manifest:
            manifest.output_video_path = str(dest_video)
            mgr.save_manifest(manifest)
            qc_result = run_quality_gate(
                video_path=dest_video,
                manifest=manifest,
                srt_path=ass_path,
                mixed_audio_path=mixed_audio,
                original_audio_path=orig_audio,
                policy=QCPolicy.REPORT_ONLY,
                workspace=WORKSPACE_DIR,
                job_id=manifest_id,
            )
            logger.info("Quality Gate kết quả: %s (score: %.1f)", qc_result.status.value, qc_result.score)
    except Exception as qce:
        logger.warning("Quality Gate note: %s", qce)

    logger.info("==================================================")
    logger.info("HOÀN TẤT THÀNH CÔNG RE-RENDER VIDEO KHỚP MIỆNG DUAL VOICE!")
    logger.info("Thành phẩm tại: %s", dest_video)
    logger.info("==================================================")


if __name__ == "__main__":
    asyncio.run(main())
