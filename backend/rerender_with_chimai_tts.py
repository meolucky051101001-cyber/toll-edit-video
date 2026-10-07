# -*- coding: utf-8 -*-
"""
rerender_with_chimai_tts.py
Re-renders previous videos with CapCut Mai (BV562_streaming - Chi Mai TTS)
instead of Chi Mai RVC.
"""

import os
import sys
import time
import shutil
import asyncio
import logging
import re
from pathlib import Path

# Ensure backend root is on sys.path
BASE_DIR = Path(r"C:\tool v1\backend")
WORKSPACE_DIR = Path(r"C:\tool v1\workspace")
OUTPUT_DIR = Path(r"D:\banve")

if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

# Ensure UTF-8 output
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)
logger = logging.getLogger("rerender_chimai_tts")

import srt
from ai.voice_cloning import generate_dubbing_audio
from ass_utils import sync_and_clamp_subtitles, generate_ass_file
from video_utils import mix_audio_pydub, process_video
import json

TARGET_VIDEOS = [
    "1790733892_c5478f32_私藏原创萌系转印贴",
    "1790734347_c8e421d0_简易物料之苹果吧唧撕拉卡",
    "1790734847_3899eb05_10小车收纳",
]

def extract_ass_meta(ass_path: Path):
    content = ass_path.read_text(encoding="utf-8")
    m_rx = re.search(r'PlayResX:\s*(\d+)', content)
    m_ry = re.search(r'PlayResY:\s*(\d+)', content)
    rx = int(m_rx.group(1)) if m_rx else 720
    ry = int(m_ry.group(1)) if m_ry else 1280
    
    y_vals = []
    for m in re.finditer(r'\\pos\((\d+),\s*(\d+)\)', content):
        y_vals.append(int(m.group(2)))
    median_y = sorted(y_vals)[len(y_vals)//2] if y_vals else int(ry * 0.88)
    main_y_pct = round(median_y / ry, 3)
    return rx, ry, main_y_pct

async def rerender_video(video_name: str):
    logger.info(f"==================================================")
    logger.info(f"BẮT ĐẦU RE-RENDER: {video_name}")
    logger.info(f"Mô hình giọng: CapCut · Mai (BV562_streaming - Chí Mai TTS)")
    logger.info(f"==================================================")
    
    folder = WORKSPACE_DIR / video_name
    video_path = WORKSPACE_DIR / "downloads" / f"{video_name}.mp4"
    srt_file = folder / "translated.srt"
    ass_file = folder / "final.ass"
    mixed_wav = folder / "mixed.wav"
    final_video = folder / f"final_{video_name}.mp4"
    dest_video = OUTPUT_DIR / f"Dubbed_{video_name}.mp4"
    dubbing_dir = folder / "dubbing"
    voice_lock_file = folder / "voice_lock.json"
    
    # Check required inputs
    if not folder.is_dir():
        raise FileNotFoundError(f"Thư mục workspace không tồn tại: {folder}")
    if not video_path.is_file():
        raise FileNotFoundError(f"File video gốc không tồn tại: {video_path}")
    if not srt_file.is_file():
        raise FileNotFoundError(f"File translated.srt không tồn tại: {srt_file}")
    
    # Find BGM (instrumental)
    roformer_dir = folder / "bs_roformer"
    bgm_path = None
    if roformer_dir.is_dir():
        for f in roformer_dir.iterdir():
            if "Instrumental" in f.name and f.suffix == ".wav":
                bgm_path = f
                break
    if not bgm_path or not bgm_path.is_file():
        raise FileNotFoundError(f"Không tìm thấy file BGM (Instrumental) trong: {roformer_dir}")
    
    # Extract resolution and subtitle position
    rx, ry, main_y_pct = extract_ass_meta(ass_file)
    logger.info(f"Thông số ASS: PlayRes={rx}x{ry}, main_y_pct={main_y_pct}")
    
    # Parse translated segments
    srt_text = srt_file.read_text(encoding="utf-8")
    translated_segments = list(srt.parse(srt_text))
    logger.info(f"Đã nạp {len(translated_segments)} đoạn phụ đề tiếng Việt từ translated.srt")
    
    # Backup old dubbing folder
    backup_dub = folder / "dubbing_rvc_backup"
    if dubbing_dir.is_dir() and not backup_dub.exists():
        logger.info(f"Sao lưu thư mục lồng tiếng cũ sang: {backup_dub.name}")
        shutil.copytree(dubbing_dir, backup_dub)
    
    # Clean dubbing dir
    shutil.rmtree(dubbing_dir, ignore_errors=True)
    dubbing_dir.mkdir(parents=True, exist_ok=True)
    
    # Get video duration
    vid_duration = None
    try:
        from pydub import AudioSegment
        orig_audio = folder / "original.wav"
        if orig_audio.is_file():
            vid_duration = len(AudioSegment.from_file(str(orig_audio))) / 1000.0
    except Exception as e:
        logger.debug(f"Không thể lấy duration: {e}")
    
    # 1. Generate Dubbing Audio with CapCut Mai TTS
    logger.info("Bước 1: Đang tạo giọng đọc AI CapCut Mai (BV562_streaming)...")
    t0 = time.time()
    dubbing_audio_files = await generate_dubbing_audio(
        translated_segments,
        str(dubbing_dir),
        voice_source="capcut",
        voice_param="BV562_streaming",
        video_duration=vid_duration,
    )
    logger.info(f"Tạo xong {len(dubbing_audio_files)} file giọng đọc trong {time.time()-t0:.2f}s")
    
    # 2. Sync and clamp subtitles
    logger.info("Bước 2: Căn chỉnh đồng bộ phụ đề với thời lượng giọng nói...")
    synced_segments = sync_and_clamp_subtitles(translated_segments, dubbing_audio_files)
    
    # 3. Generate ASS file
    logger.info(f"Bước 3: Tạo file phụ đề chuẩn final.ass (Y={main_y_pct})...")
    generate_ass_file(
        synced_segments,
        [],
        str(ass_file),
        play_res_x=rx,
        play_res_y=ry,
        main_y_pct=main_y_pct,
    )
    
    # 4. Mix Audio
    logger.info("Bước 4: Hòa âm BGM (-2.0dB) + Dubbing (+1.0dB) chế độ Soft Ducking...")
    mix_audio_pydub(
        str(bgm_path),
        dubbing_audio_files,
        str(mixed_wav),
        original_volume_db=-2.0,
        dubbing_volume_db=1.0,
        ducking_mode="soft",
        explicit=True,
    )
    
    # 5. Process / Render Video with NVENC
    logger.info("Bước 5: Render video bằng NVIDIA NVENC hardware encoder...")
    t_render = time.time()
    success = process_video(
        str(video_path),
        str(ass_file),
        str(mixed_wav),
        str(final_video),
        main_y_pct=main_y_pct,
        delogo=False,
    )
    if not success or not final_video.is_file():
        raise RuntimeError("Render video thất bại bằng process_video!")
    logger.info(f"Render hoàn tất trong {time.time()-t_render:.2f}s, dung lượng: {final_video.stat().st_size:,} bytes")
    
    # 6. Copy to D:\banve
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    shutil.copy2(str(final_video), str(dest_video))
    logger.info(f"Đã lưu video mới vào: {dest_video}")
    
    # 7. Update voice_lock.json
    try:
        lock_data = {}
        if voice_lock_file.is_file():
            lock_data = json.loads(voice_lock_file.read_text(encoding="utf-8"))
        lock_data.update({
            "voice_id": "capcut-BV562_streaming",
            "voice_source": "capcut",
            "voice_param": "BV562_streaming",
            "voice_label": "CapCut · Mai (Chí Mai TTS)",
            "rule": "user_requested_chimai_tts",
            "updated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        })
        voice_lock_file.write_text(json.dumps(lock_data, ensure_ascii=False, indent=2), encoding="utf-8")
        logger.info(f"Đã cập nhật voice_lock.json cho {video_name}")
    except Exception as exc:
        logger.warning(f"Không thể cập nhật voice_lock.json: {exc}")
    
    logger.info(f"HOÀN THÀNH XUẤT SẮC VIDEO: {video_name}!\n")

async def main():
    logger.info("=== BẮT ĐẦU XỬ LÝ LẠI 4 VIDEO VỚI GIỌNG CHÍ MAI TTS (CAPCUT MAI) ===")
    for idx, v_name in enumerate(TARGET_VIDEOS, 1):
        logger.info(f"--- Tiến độ: {idx}/{len(TARGET_VIDEOS)} ---")
        try:
            await rerender_video(v_name)
        except Exception as e:
            logger.error(f"LỖI khi xử lý {v_name}: {e}", exc_info=True)
    logger.info("=== ĐÃ HOÀN THÀNH TOÀN BỘ 4 VIDEO ===")

if __name__ == "__main__":
    asyncio.run(main())
