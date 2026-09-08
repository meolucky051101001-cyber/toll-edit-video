"""
Batch Video Processor - Tự động xử lý hàng loạt video từ thư mục máy tính (Offline / Local Folder)
- Quét toàn bộ video trong thư mục đầu vào (mặc định: D:\\video_input)
- Xử lý tuần tự từng video một để tối ưu RAM/CPU, không gây giật lag
- Xuất thành phẩm trực tiếp vào thư mục đầu ra (mặc định: D:\\banve)
"""

import os
import sys
import time
import asyncio
import logging
import gc
import shutil
from pathlib import Path

# Cấu hình UTF-8 cho console Windows
import io
if isinstance(sys.stdout, io.TextIOWrapper):
    sys.stdout.reconfigure(encoding='utf-8')
if isinstance(sys.stderr, io.TextIOWrapper):
    sys.stderr.reconfigure(encoding='utf-8')

# Đường dẫn gốc
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE_DIR)

from environment import load_environment

load_environment(Path(__file__).resolve().parent)

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
WORKSPACE = os.path.abspath(
    os.getenv("AUTODUB_WORKSPACE", os.path.join(BASE_DIR, "..", "workspace"))
)
DEFAULT_INPUT_DIR = os.path.abspath(
    os.getenv("AUTODUB_INPUT_DIR", r"D:\video_input")
)
DEFAULT_OUTPUT_DIR = os.path.abspath(
    os.getenv("AUTODUB_OUTPUT_DIR", r"D:\banve")
)
os.makedirs(WORKSPACE, exist_ok=True)

logger = logging.getLogger("batch_processor")
logger.setLevel(logging.INFO)

# Import các module AI
from ai.transcription import extract_subtitles_whisper, save_srt
from ai.translation import translate_subtitles
from ai.voice_cloning import generate_dubbing_audio
from video_utils import extract_audio_from_video, mix_audio_pydub, process_video, separate_vocals_demucs
from ass_utils import generate_ass_file
from ocr_utils import perform_video_ocr, release_ocr_reader
import shared_state

SUPPORTED_EXTENSIONS = ('.mp4', '.mkv', '.mov', '.avi', '.webm', '.flv', '.m4v')

async def process_single_local_video(video_path: str, output_dir: str, progress_callback=None) -> bool:
    """
    Quy trình 6 bước AI Dubbing cho 1 file video cục bộ
    """
    file_name = os.path.basename(video_path)
    base_name = os.path.splitext(file_name)[0]
    out_dir = os.path.join(WORKSPACE, f"batch_{base_name}")
    os.makedirs(out_dir, exist_ok=True)

    original_audio = os.path.join(out_dir, "original.wav")
    srt_original = os.path.join(out_dir, "original.srt")
    srt_translated = os.path.join(out_dir, "translated.srt")
    dubbing_dir = os.path.join(out_dir, "dubbing")
    mixed_audio = os.path.join(out_dir, "mixed.wav")
    final_video = os.path.join(out_dir, f"final_{base_name}.mp4")

    async def notify(msg: str):
        logger.info(f"[{file_name}] {msg}")
        if progress_callback:
            try:
                await progress_callback(msg)
            except Exception:
                pass

    from pipeline_v2.config import PipelineMode, PipelineSettings
    pipeline_settings = PipelineSettings.from_env()
    if pipeline_settings.mode is PipelineMode.V2:
        try:
            from pipeline_v2.video_pipeline import (
                VideoPipelineRequest,
                VideoPipelineRunner,
                discover_rvc_model,
            )

            async def v2_progress(stage: str, state: str):
                await notify("[pipeline v2] {}: {}".format(stage, state))

            final_dest = os.path.join(output_dir, f"Dubbed_{base_name}.mp4")
            rvc_model = discover_rvc_model(Path(WORKSPACE))
            request = VideoPipelineRequest(
                video_path=Path(video_path),
                job_directory=Path(out_dir),
                output_path=Path(final_dest),
                settings=pipeline_settings,
                api_key=GEMINI_API_KEY,
                voice_source="rvc" if rvc_model else "edge",
                voice_param=(
                    str(rvc_model) if rvc_model else "vi-VN-HoaiMyNeural"
                ),
                rvc_model_path=rvc_model,
                progress=v2_progress,
            )
            await VideoPipelineRunner(request).run()
            await notify("✅ Pipeline v2 hoàn thành -> {}".format(final_dest))
            return True
        except Exception as error:
            logger.error("Pipeline v2 failed: %s", error, exc_info=True)
            await notify("❌ Pipeline v2 lỗi: {}".format(error))
            return False

    try:
        t0 = time.time()
        await notify("🎧 Bước 1/6: Đang trích xuất âm thanh gốc...")
        if not extract_audio_from_video(video_path, original_audio):
            await notify("❌ Không thể trích xuất âm thanh!")
            return False

        await notify("🧠 Bước 2/6: Meta Demucs đang tách giọng nhân vật và giữ nhạc nền...")
        vocals_audio, no_vocals_audio = await asyncio.to_thread(separate_vocals_demucs, original_audio, out_dir)

        await notify("🤖 Bước 3/6: Whisper Large-v3 AI đang nhận dạng giọng nói...")
        srt_segments = await asyncio.to_thread(extract_subtitles_whisper, vocals_audio, srt_original)
        if not srt_segments:
            await notify("⚠️ Video không có giọng nói để dịch!")
            return False

        await notify("👀 Bước 3.5/6: Đang quét vị trí phụ đề gốc...")
        try:
            _, vid_w, vid_h, main_y_pct = await asyncio.to_thread(
                perform_video_ocr, video_path, target_lang="vi", sample_rate=1.0, api_key=GEMINI_API_KEY, srt_segments=srt_segments
            )
        except Exception as e:
            logger.warning(f"OCR Warning: {e}")
            vid_w, vid_h, main_y_pct = 1080, 1920, 0.88
        finally:
            release_ocr_reader()

        await notify(f"🌐 Bước 4/6: Gemini 3.7 Flash đang dịch ({len(srt_segments)} câu)...")
        translated_segments = await asyncio.to_thread(translate_subtitles, srt_segments, "vi", api_key=GEMINI_API_KEY, video_path=video_path)
        await asyncio.to_thread(save_srt, translated_segments, srt_translated)

        await notify("🗣️ Bước 5/6: Microsoft Neural TTS đang lồng tiếng AI...")
        dubbing_audio_files = await generate_dubbing_audio(
            translated_segments, dubbing_dir, voice_source="edge", voice_param="vi-VN-HoaiMyNeural"
        )

        # Căn chỉnh phụ đề ASS
        ass_path = os.path.join(out_dir, "final.ass")
        await asyncio.to_thread(generate_ass_file, translated_segments, [], ass_path, play_res_x=vid_w, play_res_y=vid_h, main_y_pct=main_y_pct)

        # Trộn nhạc nền sạch với giọng lồng tiếng
        await asyncio.to_thread(mix_audio_pydub, no_vocals_audio, dubbing_audio_files, mixed_audio, original_volume_db=-2, dubbing_volume_db=1)

        await notify("🎬 Bước 6/6: Đang Render video thành phẩm (Multi-threading)...")
        res = await asyncio.to_thread(process_video, video_path, ass_path, mixed_audio, final_video, main_y_pct=main_y_pct)
        if not res or not os.path.exists(final_video):
            await notify("❌ Lỗi trong quá trình render video!")
            return False

        # Lưu thành phẩm vào thư mục đầu ra
        os.makedirs(output_dir, exist_ok=True)
        final_dest = os.path.join(output_dir, f"Dubbed_{base_name}.mp4")
        shutil.copy2(final_video, final_dest)

        if pipeline_settings.mode is PipelineMode.SHADOW:
            try:
                from pipeline_v2.shadow import snapshot_completed_legacy_run

                snapshot_completed_legacy_run(
                    Path(video_path),
                    Path(out_dir) / "pipeline_v2_shadow",
                    {
                        "extract_audio": {"original_audio": Path(original_audio)},
                        "demucs": {
                            "vocals": Path(vocals_audio),
                            "background": Path(no_vocals_audio),
                        },
                        "transcribe": {"srt": Path(srt_original)},
                        "translate": {"srt": Path(srt_translated)},
                        "tts": {"dubbing_directory": Path(dubbing_dir)},
                        "mix": {"mixed_audio": Path(mixed_audio)},
                        "render": {"final_video": Path(final_video)},
                        "deliver": {"published_video": Path(final_dest)},
                    },
                    run_started_at_epoch=t0,
                )
            except Exception as shadow_error:
                logger.warning("Shadow manifest warning: %s", shadow_error)

        dt = int(time.time() - t0)
        await notify(f"✅ Hoàn thành video ({dt}s) -> Đã lưu vào {final_dest}")
        return True

    except Exception as e:
        logger.error(f"Lỗi xử lý file {file_name}: {e}", exc_info=True)
        await notify(f"❌ Lỗi: {str(e)}")
        return False
    finally:
        # Giải phóng bộ nhớ RAM/VRAM ngay lập tức sau mỗi video
        gc.collect()
        try:
            import torch
            if torch.cuda.is_available():
                torch.cuda.empty_cache()
        except:
            pass

async def process_batch_folder(
    input_dir: str = DEFAULT_INPUT_DIR,
    output_dir: str = DEFAULT_OUTPUT_DIR,
    progress_callback=None,
):
    """
    Quét và xử lý toàn bộ video trong thư mục đầu vào
    """
    if not os.path.exists(input_dir):
        os.makedirs(input_dir, exist_ok=True)
        msg = f"📁 Đã tạo thư mục đầu vào: `{input_dir}`. Bạn hãy thả các video cần edit vào đây nhé!"
        logger.info(msg)
        if progress_callback:
            await progress_callback(msg)
        return

    os.makedirs(output_dir, exist_ok=True)
    video_files = [
        os.path.join(input_dir, f) for f in os.listdir(input_dir)
        if f.lower().endswith(SUPPORTED_EXTENSIONS) and not f.startswith("Dubbed_")
    ]

    if not video_files:
        msg = f"⚠️ Không tìm thấy video nào trong `{input_dir}`. Hãy thả file video (.mp4, .mkv, .mov...) vào đây!"
        logger.info(msg)
        if progress_callback:
            await progress_callback(msg)
        return

    total = len(video_files)
    start_msg = f"🚀 Bắt đầu xử lý hàng loạt **{total} video** từ thư mục:\n📂 `{input_dir}`\n💾 Đầu ra: `{output_dir}`"
    logger.info(start_msg)
    if progress_callback:
        await progress_callback(start_msg)

    # Thư mục lưu các video gốc đã xử lý xong
    processed_archive = os.path.join(input_dir, "processed")
    os.makedirs(processed_archive, exist_ok=True)

    success_count = 0
    for idx, vpath in enumerate(video_files, 1):
        vname = os.path.basename(vpath)
        step_msg = f"🎬 **[{idx}/{total}] Đang xử lý:** `{vname}`..."
        logger.info(step_msg)
        if progress_callback:
            await progress_callback(step_msg)

        ok = await process_single_local_video(vpath, output_dir, progress_callback)
        if ok:
            success_count += 1
            # Di chuyển file gốc đã làm xong sang thư mục processed để không bị trùng lặp
            try:
                dest_archive = os.path.join(processed_archive, vname)
                shutil.move(vpath, dest_archive)
            except Exception as mv_err:
                logger.warning(f"Không thể di chuyển file gốc: {mv_err}")

    summary_msg = f"🎉 **ĐÃ HOÀN TẤT XỬ LÝ HÀNG LOẠT!**\n✅ Thành công: {success_count}/{total} video\n💾 Thư mục lưu thành phẩm: `{output_dir}`"
    logger.info(summary_msg)
    if progress_callback:
        await progress_callback(summary_msg)

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Batch Video Dubbing Processor")
    parser.add_argument("--input", default=DEFAULT_INPUT_DIR, help="Thư mục chứa video gốc")
    parser.add_argument("--output", default=DEFAULT_OUTPUT_DIR, help="Thư mục lưu video thành phẩm")
    args = parser.parse_args()

    asyncio.run(process_batch_folder(args.input, args.output))
