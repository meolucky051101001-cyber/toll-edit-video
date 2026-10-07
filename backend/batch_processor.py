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
    os.getenv("AUTODUB_OUTPUT_DIR", r"D:\video tool v2")
)
os.makedirs(WORKSPACE, exist_ok=True)

logger = logging.getLogger("batch_processor")
logger.setLevel(logging.INFO)
try:
    from social_downloader import SensitiveUrlFilter
    logger.addFilter(SensitiveUrlFilter())
except Exception:
    pass

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

    # Hash source video to assign unique job_id and prevent collision
    try:
        from pipeline_v2.artifact_store import hash_file
        source_sha256, _ = hash_file(video_path)
        source_hash_short = source_sha256[:10]
    except Exception:
        source_sha256 = ""
        source_hash_short = "local"

    job_id = f"batch_{source_hash_short}_{base_name}"
    out_dir = os.path.join(WORKSPACE, job_id)
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
    from pipeline_v2.delivery_verification import verify_delivered_product
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

            # Prevent overwriting an existing output that belongs to a different video
            default_dest_name = f"Dubbed_{base_name}.mp4"
            final_dest = os.path.join(output_dir, default_dest_name)
            if os.path.isfile(final_dest) and source_sha256:
                existing_ver = verify_delivered_product(
                    Path(out_dir),
                    expected_output_path=final_dest,
                    expected_source_sha256=source_sha256,
                    check_sha256=False,
                    verify_media_streams=False,
                )
                if not existing_ver.is_valid:
                    final_dest = os.path.join(output_dir, f"Dubbed_{base_name}_{source_hash_short[:6]}.mp4")

            rvc_model = discover_rvc_model(Path(WORKSPACE))
            from voice_selection import resolve_voice, get_speaker_voice_map, get_speaker_map, is_dual_voice_enabled
            from dataclasses import replace
            from pipeline_v2.config import QCGatePolicy
            dual_voice_on = is_dual_voice_enabled()
            pipeline_settings = replace(pipeline_settings, enable_adaptive_ocr=True, enable_auto_gender=dual_voice_on)
            selected_source, selected_param, selected_label = resolve_voice("rvc" if rvc_model else "edge", rvc_model)
            request = VideoPipelineRequest(
                video_path=Path(video_path),
                job_directory=Path(out_dir),
                output_path=Path(final_dest),
                settings=pipeline_settings,
                api_key=GEMINI_API_KEY,
                voice_source=selected_source,
                voice_param=selected_param,
                rvc_model_path=Path(selected_param) if selected_source == "rvc" else rvc_model,
                speaker_map=get_speaker_map() if dual_voice_on else None,
                speaker_voice_map=get_speaker_voice_map() if dual_voice_on else None,
                progress=v2_progress,
            )
            await VideoPipelineRunner(request).run()

            # Strictly verify delivered product before claiming success
            ver_result = verify_delivered_product(
                Path(out_dir),
                expected_output_path=final_dest,
                expected_source_sha256=source_sha256 or None,
                qc_policy=pipeline_settings.qc_gate_policy,
                check_sha256=True,
                verify_media_streams=True,
            )
            if ver_result.is_valid:
                await notify("✅ Pipeline v2 hoàn thành và xác minh thành công -> {}".format(final_dest))
                return True
            else:
                logger.error("Pipeline v2 giao hàng thất bại do không đạt xác minh: %s", ver_result.reason)
                await notify("❌ Pipeline v2 thất bại: {}".format(ver_result.reason))
                return False
        except Exception as error:
            is_qc_block = "QC gate" in str(error) or error.__class__.__name__ == "QCGateBlocked"
            if is_qc_block and getattr(pipeline_settings, "qc_gate_policy", None) == QCGatePolicy.BLOCK:
                logger.error("Pipeline v2 bị chặn do vi phạm tiêu chuẩn QC nghiêm ngặt: %s", error)
                await notify("❌ Pipeline v2 thất bại: Vi phạm tiêu chuẩn QC ({})".format(error))
                return False
            logger.error("Pipeline v2 failed: %s", error, exc_info=True)
            await notify("❌ Pipeline v2 lỗi: {}".format(error))
            return False

    try:
        t0 = time.time()
        # ===== TỰ ĐỘNG GỠ WATERMARK NẾU ĐƯỢC KÍCH HOẠT =====
        try:
            from watermark_api import get_watermark_config
            wm_cfg = get_watermark_config()
            if wm_cfg.get("auto_remove"):
                await notify("🪄 Đang tự động gỡ Watermark AI khỏi video gốc...")
                from ai.watermark_removal_service import get_watermark_service
                clean_source_path = os.path.join(out_dir, "clean_source.mp4")
                wm_res = await asyncio.to_thread(
                    get_watermark_service().process_video,
                    input_path=video_path,
                    output_path=clean_source_path,
                    mode=wm_cfg.get("mode", "both"),
                    gain=float(wm_cfg.get("gain", 0.6)),
                )
                if wm_res.get("success") and os.path.isfile(clean_source_path):
                    video_path = clean_source_path
                    await notify("✅ Đã gỡ sạch Watermark AI thành công!")
        except Exception as _wme:
            logger.warning("Bỏ qua lỗi tự động gỡ watermark: %s", _wme)

        # ===== BƯỚC 1/4: TÁCH ÂM THANH & NHẠC NỀN GỐC =====
        await notify("🎧 Bước 1/4: Đang trích xuất & tách âm thanh (BS-RoFormer GPU)...")
        if not extract_audio_from_video(video_path, original_audio):
            await notify("❌ Không thể trích xuất âm thanh!")
            return False

        vocals_audio, no_vocals_audio = await asyncio.to_thread(separate_vocals_demucs, original_audio, out_dir)

        # ===== BƯỚC 2/4: NHẬN DIỆN GIỌNG NÓI & DỊCH THUẬT AI =====
        await notify("🤖 Bước 2/4: Nhận diện giọng nói & Dịch thuật AI (Whisper + Gemini)...")
        srt_segments = await asyncio.to_thread(extract_subtitles_whisper, vocals_audio, srt_original)
        if not srt_segments:
            await notify("⚠️ Video không có giọng nói để dịch!")
            return False

        # Xác định kích thước video & vị trí phụ đề chuẩn trong 0.01s (bỏ qua quét OCR rườm rà)
        try:
            import cv2
            _cap = cv2.VideoCapture(video_path)
            vid_w = int(_cap.get(cv2.CAP_PROP_FRAME_WIDTH)) or 1080
            vid_h = int(_cap.get(cv2.CAP_PROP_FRAME_HEIGHT)) or 1920
            _cap.release()
        except Exception:
            vid_w, vid_h = 1080, 1920
        main_y_pct = 0.88 if vid_h > vid_w else 0.85

        translated_segments = await asyncio.to_thread(translate_subtitles, srt_segments, "vi", api_key=GEMINI_API_KEY, video_path=video_path)
        await asyncio.to_thread(save_srt, translated_segments, srt_translated)

        # ===== BƯỚC 3/4: LỒNG TIẾNG AI & HÒA ÂM STUDIO =====
        frozen_job_entry = None
        try:
            from job_config_service import get_frozen_config
            frozen_job_entry = get_frozen_config(file_name)
        except Exception:
            pass

        frozen_eff = frozen_job_entry.get("effective_config", {}) if frozen_job_entry else {}
        frozen_voice_id = frozen_eff.get("voice_id")

        v2_source = "edge"
        v2_param = "vi-VN-HoaiMyNeural"
        v2_label = "Hoài My"
        if frozen_voice_id:
            from voice_selection import catalog
            cat = catalog()
            v_match = next((v for v in cat if v["id"] == frozen_voice_id), None)
            if v_match:
                v2_source = v_match["source"]
                v2_param = v_match["param"]
                v2_label = v_match["label"]
            logger.info("V2 sử dụng giọng đóng băng theo job cho %s: %s (%s)", file_name, v2_label, frozen_voice_id)
        else:
            try:
                from voice_selection import selected
                curr_v = selected()
                v2_source = curr_v.get("source", "edge")
                v2_param = curr_v.get("param", "vi-VN-HoaiMyNeural")
                v2_label = curr_v.get("label", "Hoài My")
            except Exception:
                pass

        await notify(f"🗣️ Bước 3/4: Lồng tiếng AI ({v2_label}, {len(translated_segments)} câu) & Hòa âm trong trẻo...")
        dubbing_audio_files = await generate_dubbing_audio(
            translated_segments, dubbing_dir, voice_source=v2_source, voice_param=v2_param
        )

        # Căn chỉnh phụ đề ASS
        ass_path = os.path.join(out_dir, "final.ass")
        await asyncio.to_thread(generate_ass_file, translated_segments, [], ass_path, play_res_x=vid_w, play_res_y=vid_h, main_y_pct=main_y_pct)

        # Bảo tồn âm nền trong trẻo nguyên bản ở khoảng không thoại & phục hồi treble >14kHz
        try:
            from ai.audio_enhancer import preserve_pristine_background
            pristine_bgm = os.path.join(out_dir, "pristine_background.wav")
            enhanced_bgm = await asyncio.to_thread(
                preserve_pristine_background,
                original_audio,
                no_vocals_audio,
                srt_segments,
                pristine_bgm,
            )
            if os.path.isfile(enhanced_bgm):
                no_vocals_audio = enhanced_bgm
        except Exception as enh_err:
            logger.warning(f"Selective background preservation notice: {enh_err}")

        # Trộn nhạc nền sạch với giọng lồng tiếng (Dynamic Sidechain Ducking & EBU R128)
        v2_bgm_vol = frozen_eff.get("bgm_volume_db", -2.0)
        v2_dub_vol = frozen_eff.get("dubbing_volume_db", 1.0)
        await asyncio.to_thread(
            mix_audio_pydub,
            no_vocals_audio,
            dubbing_audio_files,
            mixed_audio,
            original_volume_db=v2_bgm_vol,
            dubbing_volume_db=v2_dub_vol,
            explicit=True,
        )

        # ===== BƯỚC 4/4: RENDER VIDEO THÀNH PHẨM =====
        await notify("🎬 Bước 4/4: Đang Render video thành phẩm (NVENC GPU)...")
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

    def _safe_archive_source(vpath: str, base_stem: str, vname: str, job_dir: Optional[str] = None) -> bool:
        dest_archive = os.path.join(processed_archive, vname)
        if os.path.exists(dest_archive):
            dest_archive = os.path.join(processed_archive, f"{base_stem}_{int(time.time())}{os.path.splitext(vname)[1]}")
        max_retries = 3
        for attempt in range(max_retries):
            try:
                shutil.move(vpath, dest_archive)
                logger.info("Đã lưu trữ video nguồn hoàn thành: %s -> %s", vpath, dest_archive)
                return True
            except (OSError, PermissionError) as err:
                logger.warning("Thử di chuyển video nguồn lần %d/%d thất bại (%s): %s", attempt + 1, max_retries, err, vpath)
                time.sleep(0.5)

        logger.warning("Không thể di chuyển file nguồn do lock Windows. Đánh dấu delivered_archive_pending: %s", vpath)
        if job_dir and os.path.isdir(job_dir):
            try:
                marker = os.path.join(job_dir, "archive_status.json")
                with open(marker, "w", encoding="utf-8") as f:
                    json.dump({
                        "status": "delivered_archive_pending",
                        "source_path": vpath,
                        "target_archive": dest_archive,
                        "marked_at": time.time(),
                    }, f, indent=2)
            except Exception:
                pass
        return False

    from pipeline_v2.artifact_store import hash_file
    from pipeline_v2.delivery_verification import verify_delivered_product

    # Kiểm tra và xử lý các video có trạng thái delivered_archive_pending trước
    for pending_file in list(video_files):
        if not os.path.isfile(pending_file):
            continue
        try:
            p_sha, _ = hash_file(pending_file)
            p_stem = os.path.splitext(os.path.basename(pending_file))[0]
            p_job_dir = os.path.join(WORKSPACE, f"batch_{p_sha[:10]}_{p_stem}")
            marker = os.path.join(p_job_dir, "archive_status.json")
            if os.path.isfile(marker):
                ver = verify_delivered_product(p_job_dir, expected_source_sha256=p_sha, check_sha256=False, verify_media_streams=False)
                if ver.is_valid:
                    if _safe_archive_source(pending_file, p_stem, os.path.basename(pending_file), p_job_dir):
                        try:
                            os.remove(marker)
                        except Exception:
                            pass
        except Exception:
            pass

    # Làm mới danh sách video cần xử lý sau khi hoàn tất các pending archive
    video_files = [
        os.path.join(input_dir, f) for f in os.listdir(input_dir)
        if f.lower().endswith(SUPPORTED_EXTENSIONS) and not f.startswith("Dubbed_") and os.path.isfile(os.path.join(input_dir, f))
    ]
    total = len(video_files)
    if total == 0:
        summary_msg = f"🎉 **ĐÃ HOÀN TẤT!**\nToàn bộ video đã được xác minh thành phẩm và lưu trữ."
        logger.info(summary_msg)
        if progress_callback:
            await progress_callback(summary_msg)
        return

    success_count = 0
    for idx, vpath in enumerate(video_files, 1):
        vname = os.path.basename(vpath)
        base_stem = os.path.splitext(vname)[0]

        # Kiểm tra nếu video này đã có job V2 hoàn thành và xác minh thành phẩm hợp lệ
        try:
            source_sha, _ = hash_file(vpath)
            source_hash_short = source_sha[:10]
        except Exception:
            source_sha = ""
            source_hash_short = ""

        job_dir = os.path.join(WORKSPACE, f"batch_{source_hash_short}_{base_stem}") if source_hash_short else os.path.join(WORKSPACE, f"batch_{base_stem}")
        existing_ver = verify_delivered_product(
            job_dir,
            expected_source_sha256=source_sha or None,
            check_sha256=True,
            verify_media_streams=True,
        ) if os.path.isdir(job_dir) else None

        if existing_ver and existing_ver.is_valid:
            delivered_file = existing_ver.published_outputs[0]["path"] if existing_ver.published_outputs else f"Dubbed_{base_stem}.mp4"
            skip_msg = f"⏩ [{idx}/{total}] Video `{vname}` đã có thành phẩm xác minh hợp lệ (`{os.path.basename(delivered_file)}`). Di chuyển vào thư mục processed..."
            logger.info(skip_msg)
            if progress_callback:
                await progress_callback(skip_msg)
            if _safe_archive_source(vpath, base_stem, vname, job_dir):
                success_count += 1
            continue

        step_msg = f"🎬 **[{idx}/{total}] Đang xử lý:** `{vname}`..."
        logger.info(step_msg)
        if progress_callback:
            await progress_callback(step_msg)

        ok = await process_single_local_video(vpath, output_dir, progress_callback)
        if ok:
            success_count += 1
            _safe_archive_source(vpath, base_stem, vname, job_dir)
        else:
            logger.warning("Video %s thất bại hoặc bị chặn QC. Giữ nguyên video nguồn trong thư mục chờ.", vname)
            if progress_callback:
                await progress_callback(f"⚠️ Video `{vname}` thất bại hoặc bị chặn QC. Giữ nguyên trong thư mục chờ để retry.")

    summary_msg = f"🎉 **ĐÃ HOÀN TẤT XỬ LÝ HÀNG LOẠT!**\n✅ Thành công: {success_count}/{total} video\n💾 Thư mục lưu thành phẩm: `{output_dir}`"
    logger.info(summary_msg)
    if progress_callback:
        await progress_callback(summary_msg)

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Batch Video Dubbing Processor")
    parser.add_argument("--input", default=DEFAULT_INPUT_DIR, help="Thư mục chứa video gốc")
    parser.add_argument("--output", default=DEFAULT_OUTPUT_DIR, help="Thư mục lưu video thành phẩm")
    parser.add_argument("--video", default="", help="File video cụ thể cần xử lý lại")
    args = parser.parse_args()

    if args.video and os.path.isfile(args.video):
        asyncio.run(process_single_local_video(args.video, args.output))
    else:
        asyncio.run(process_batch_folder(args.input, args.output))
