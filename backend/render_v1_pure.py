import asyncio
import os
import sys
import time
from pathlib import Path

# Fix Windows console UTF-8 encoding
if hasattr(sys.stdout, "reconfigure"):
    try: sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception: pass
if hasattr(sys.stderr, "reconfigure"):
    try: sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception: pass

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE_DIR)

env_file = os.path.join(BASE_DIR, ".env")
if os.path.exists(env_file):
    with open(env_file, "r", encoding="utf-8") as f:
        for line in f:
            if "=" in line and not line.strip().startswith("#"):
                k, v = line.strip().split("=", 1)
                os.environ[k.strip()] = v.strip().strip('"').strip("'")

# Force Legacy Mode
os.environ["PIPELINE_MODE"] = "legacy"

from ai.transcription import extract_subtitles_whisper
from ai.translation import translate_subtitles
from ai.voice_cloning import generate_dubbing_audio
from video_utils import extract_audio_from_video, mix_audio_pydub, process_video, separate_vocals_demucs
from ass_utils import generate_ass_file
from ocr_utils import perform_video_ocr, release_ocr_reader

async def run_tool_v1(video_path: str, output_dir: str):
    file_name = os.path.basename(video_path)
    base_name = os.path.splitext(file_name)[0]
    workspace = r"C:\Users\admin\.gemini\antigravity\scratch\video-dubbing-app\workspace"
    out_dir = os.path.join(workspace, f"batch_{base_name}")
    os.makedirs(out_dir, exist_ok=True)

    original_audio = os.path.join(out_dir, "original.wav")
    srt_original = os.path.join(out_dir, "original.srt")
    srt_translated = os.path.join(out_dir, "translated.srt")
    dubbing_dir = os.path.join(out_dir, "dubbing")
    mixed_audio = os.path.join(out_dir, "mixed.wav")
    final_video = os.path.join(out_dir, f"final_{base_name}.mp4")
    final_dest = os.path.join(output_dir, f"Dubbed_{base_name}.mp4")

    print(f"\n🎬 [Tool V1] BẮT ĐẦU XỬ LÝ: {file_name}", flush=True)
    t0 = time.time()
    
    print("🎧 [Tool V1] Bước 1/6: Đang trích xuất âm thanh gốc...", flush=True)
    extract_audio_from_video(video_path, original_audio)

    print("🧠 [Tool V1] Bước 2/6: Meta Demucs đang tách giọng nhân vật và giữ nhạc nền...", flush=True)
    vocals_audio, no_vocals_audio = await asyncio.to_thread(separate_vocals_demucs, original_audio, out_dir)

    print("🤖 [Tool V1] Bước 3/6: Whisper Large-v3 AI đang nhận dạng giọng nói...", flush=True)
    srt_segments = await asyncio.to_thread(extract_subtitles_whisper, vocals_audio, srt_original)
    if not srt_segments:
        print("⚠️ Video không có giọng nói để dịch!", flush=True)
        return False

    print("👀 [Tool V1] Bước 3.5/6: Đang quét vị trí phụ đề gốc...", flush=True)
    main_y_pct = 0.85
    try:
        _, _, _, main_y_pct = await asyncio.to_thread(perform_video_ocr, video_path, 'vi', 1.0, None, srt_segments)
    finally:
        release_ocr_reader()

    print("🌐 [Tool V1] Bước 4/6: Dịch phụ đề sang Tiếng Việt...", flush=True)
    translated_segments = await asyncio.to_thread(translate_subtitles, srt_segments, 'vi', os.getenv("GEMINI_API_KEY", ""), video_path)

    print("🎤 [Tool V1] Bước 5/6: Lồng tiếng AI (Chí Mai RVC v1)...", flush=True)
    dubbing_audio_files = await generate_dubbing_audio(translated_segments, dubbing_dir, 'rvc')

    print("🎨 [Tool V1] Tạo phụ đề ASS chuẩn xác...", flush=True)
    ass_path = os.path.join(out_dir, "subtitles.ass")
    generate_ass_file(translated_segments, [], ass_path, 1080, 1920, main_y_pct)

    print("🎚️ [Tool V1] Hòa âm trộn nhạc nền...", flush=True)
    await asyncio.to_thread(mix_audio_pydub, no_vocals_audio, dubbing_audio_files, mixed_audio, -2, 1)

    print("🚀 [Tool V1] Bước 6/6: Render video Full HD NVENC...", flush=True)
    os.makedirs(output_dir, exist_ok=True)
    success = await asyncio.to_thread(process_video, video_path, ass_path, mixed_audio, final_dest, main_y_pct=main_y_pct)

    total_time = int(time.time() - t0)
    print(f"🎉 [Tool V1] HOÀN TẤT trong {total_time}s ({total_time//60}p {total_time%60}s)!", flush=True)
    print(f"💾 Đã lưu thành phẩm vào: {final_dest}\n", flush=True)
    return success

async def main():
    video_path = r"D:\video phôi\1788280024245_video.mp4"
    output_dir = r"D:\banve"
    await run_tool_v1(video_path, output_dir)

if __name__ == "__main__":
    asyncio.run(main())
