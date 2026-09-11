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
import uuid
import hashlib
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

# Load biến môi trường từ .env
env_file = os.path.join(BASE_DIR, ".env")
if os.path.exists(env_file):
    with open(env_file, "r", encoding="utf-8") as f:
        for line in f:
            if "=" in line and not line.strip().startswith("#"):
                k, v = line.strip().split("=", 1)
                os.environ[k.strip()] = v.strip().strip('"').strip("'")

# The Tool V1 batch entrypoint is permanently isolated from Pipeline V2.
os.environ["PIPELINE_MODE"] = "legacy"

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
WORKSPACE = os.path.abspath(
    os.getenv("AUTODUB_WORKSPACE", os.path.join(BASE_DIR, "..", "workspace"))
)
def get_default_input_dir():
    env_dir = os.getenv("AUTODUB_INPUT_DIR")
    if env_dir:
        return os.path.abspath(env_dir)
    for candidate in [r"D:\video phôi", r"D:\video phoi", r"D:\video_input"]:
        if os.path.exists(candidate):
            return os.path.abspath(candidate)
    return os.path.abspath(r"D:\video phôi")

DEFAULT_INPUT_DIR = get_default_input_dir()
DEFAULT_OUTPUT_DIR = os.path.abspath(
    os.getenv("AUTODUB_OUTPUT_DIR", r"D:\banve")
)
os.makedirs(WORKSPACE, exist_ok=True)

logger = logging.getLogger("batch_processor")
logger.setLevel(logging.INFO)

# Import các module AI
from ai.transcription import extract_subtitles_whisper, save_srt
from ai.translation import translate_subtitles
from ai.voice_cloning import generate_dubbing_audio, rvc_runtime_available
from video_utils import extract_audio_from_video, mix_audio_pydub, process_video, separate_vocals_demucs
from ass_utils import generate_ass_file
from ocr_utils import perform_video_ocr, release_ocr_reader
import shared_state
import job_tracker
from pipeline_v2.atomic_io import atomic_copy_file, atomic_write_json
from batch_checkpoint import fingerprint
from pipeline_v2.download_validation import probe_downloaded_video
import json

SUPPORTED_EXTENSIONS = ('.mp4', '.mkv', '.mov', '.avi', '.webm', '.flv', '.m4v')


class BatchStopRequested(RuntimeError):
    """Raised at safe checkpoints when the user asks the batch to stop."""


def _stop_requested() -> bool:
    return bool(
        getattr(shared_state, "stop_requested", False)
        or job_tracker.is_stop_requested()
    )


def _raise_if_stopped() -> None:
    if _stop_requested():
        raise BatchStopRequested("Batch đã được dừng theo yêu cầu")


def _receipt_path(output):
    import hashlib
    key = os.path.normcase(os.path.abspath(str(output)))
    name = hashlib.sha256(key.encode("utf-8")).hexdigest() + ".json"
    return Path(WORKSPACE) / "output_receipts" / name


def _verified_output(source, output):
    try:
        receipt = json.loads(_receipt_path(output).read_text(encoding="utf-8"))
        return (receipt["input_sha256"] == fingerprint(source)
                and receipt["output_sha256"] == fingerprint(output))
    except (OSError, ValueError, KeyError, TypeError):
        return False


def _archive_source(source, archive):
    """Never overwrite an existing original in the archive."""
    destination = Path(archive) / Path(source).name
    if destination.exists():
        destination = destination.with_name(destination.stem + "_" + uuid.uuid4().hex + destination.suffix)
    shutil.move(source, destination)


def _cleanup_job_directory(path: str) -> None:
    """Remove only a direct, Tool-owned batch directory inside WORKSPACE."""
    workspace = Path(WORKSPACE).resolve()
    candidate = Path(path).resolve()
    if candidate.parent != workspace or not candidate.name.startswith("batch_"):
        raise RuntimeError(f"Từ chối xóa thư mục tạm không an toàn: {candidate}")
    marker = candidate / ".autodub-owned"
    if not marker.is_file() or marker.read_text(encoding="utf-8") != str(candidate):
        raise RuntimeError("Thư mục thiếu xác nhận sở hữu của batch.")
    if candidate.exists():
        shutil.rmtree(candidate)


async def process_single_local_video(video_path: str, output_dir: str, progress_callback=None,
                                     queue_index=None, queue_total=None) -> bool:
    """
    Quy trình 6 bước AI Dubbing cho 1 file video cục bộ
    """
    file_name = os.path.basename(video_path)
    status = job_tracker.get_status()
    if queue_index is not None or not status.get("active") or status.get("video_name") != file_name:
        job_tracker.start_video(file_name, queue_index or 1, queue_total or 1)
    base_name = os.path.splitext(file_name)[0]
    source_id = hashlib.sha256(str(Path(video_path).resolve()).encode()).hexdigest()[:16]
    out_dir = os.path.join(WORKSPACE, f"batch_{source_id}_{base_name}")
    if Path(out_dir).resolve().parent != Path(WORKSPACE).resolve():
        raise RuntimeError("Thư mục batch nằm ngoài workspace.")
    os.makedirs(out_dir, exist_ok=True)
    from pipeline_v2.atomic_io import atomic_write_text
    atomic_write_text(Path(out_dir) / ".autodub-owned", str(Path(out_dir).resolve()))
    from batch_checkpoint import Checkpoints
    checkpoints = await asyncio.to_thread(Checkpoints, out_dir, video_path)

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
        _raise_if_stopped()
        await notify("🎧 Bước 1/6: Đang trích xuất âm thanh gốc...")
        job_tracker.update_step(1, "Bước 1/6: Đang trích xuất âm thanh gốc...", percent=10)
        if not await asyncio.to_thread(extract_audio_from_video, video_path, original_audio):
            await notify("❌ Không thể trích xuất âm thanh!")
            job_tracker.set_error(
                file_name, "Không thể trích xuất âm thanh", fatal=False
            )
            return False

        _raise_if_stopped()
        await notify("🧠 Bước 2/6: Demucs htdemucs Fast đang tách giọng và giữ nhạc nền...")
        job_tracker.update_step(2, "Bước 2/6: Demucs tách giọng và giữ nhạc nền...", percent=25)
        vocals_audio, no_vocals_audio = await asyncio.to_thread(separate_vocals_demucs, original_audio, out_dir)

        _raise_if_stopped()
        await notify("🤖 Bước 3/6: Faster-Whisper Large-v3 Turbo đang nhận dạng giọng nói...")
        job_tracker.update_step(3, "Bước 3/6: Faster-Whisper Large-v3 Turbo nhận dạng giọng nói...", percent=40)
        srt_segments = await checkpoints.subtitles(
            "transcribe", vocals_audio,
            lambda: asyncio.to_thread(extract_subtitles_whisper, vocals_audio, srt_original),
            srt_original)
        if not srt_segments:
            await notify("⚠️ Video không có giọng nói để dịch!")
            job_tracker.set_error(
                file_name, "Video không có giọng nói để dịch", fatal=False
            )
            return False

        _raise_if_stopped()
        await notify("👀 Bước 3.5/6: Đang quét vị trí phụ đề gốc...")
        job_tracker.update_step(3.5, "Bước 3.5/6: Quét vị trí phụ đề gốc (PP-OCRv6)...", percent=55)
        try:
            _, vid_w, vid_h, main_y_pct = await asyncio.to_thread(
                perform_video_ocr, video_path, target_lang="vi", sample_rate=1.0, api_key=GEMINI_API_KEY, srt_segments=srt_segments
            )
        except Exception as e:
            logger.warning(f"OCR Warning: {e}")
            vid_w, vid_h, main_y_pct = 1080, 1920, 0.88
        finally:
            release_ocr_reader()

        # XỬ LÝ THỜI GIAN CHUẨN KHI LÀM SUB & LỒNG TIẾNG (Chống lệch giọng)
        import datetime
        for i in range(len(srt_segments) - 1):
            if srt_segments[i].end > srt_segments[i+1].start:
                new_end = srt_segments[i+1].start - datetime.timedelta(seconds=0.05)
                if new_end > srt_segments[i].start:
                    srt_segments[i].end = new_end
                else:
                    srt_segments[i].end = srt_segments[i].start + datetime.timedelta(seconds=0.1)

        for i, seg in enumerate(srt_segments, 1):
            seg.index = i

        _raise_if_stopped()
        await notify(f"🌐 Bước 4/6: Gemini 3.8 Flash đang dịch ({len(srt_segments)} câu)...")
        job_tracker.update_step(4, f"Bước 4/6: Gemini 3.8 Flash đang dịch ({len(srt_segments)} câu)...", percent=70)
        translated_segments = await checkpoints.subtitles(
            "translate", srt_original,
            lambda: asyncio.to_thread(translate_subtitles, srt_segments, "vi",
                                      api_key=GEMINI_API_KEY, video_path=video_path),
            srt_translated)
        await asyncio.to_thread(save_srt, translated_segments, srt_translated)

        # Khôi phục giọng RVC (Đáng yêu / Chí Mai)
        rvc_model_path = None
        search_dirs = [
            os.path.join(os.path.dirname(__file__), "..", "MyVoiceModel_v2"),
            os.path.join(WORKSPACE, "..", "MyVoiceModel_v2"),
            os.path.join(WORKSPACE, "MyVoiceModel_v2"),
            os.path.join(WORKSPACE, "models", "rvc"),
            os.path.join(os.path.dirname(__file__), "..", "models", "rvc"),
        ]
        for d in search_dirs:
            if os.path.exists(d):
                for f in sorted(os.listdir(d)):
                    if f.endswith(".pth"):
                        candidate = os.path.join(d, f)
                        try:
                            if os.path.getsize(candidate) > 1024:
                                rvc_model_path = candidate
                                break
                        except OSError:
                            continue
            if rvc_model_path:
                break
                
        v_source = "rvc" if rvc_model_path and rvc_runtime_available() else "edge"
        v_param = rvc_model_path if v_source == "rvc" else "vi-VN-HoaiMyNeural"

        _raise_if_stopped()
        await notify(f"🗣️ Bước 5/6: Đang lồng tiếng AI ({'Giọng Chí Mai RVC' if v_source == 'rvc' else 'Giọng Hoài My'})...")
        job_tracker.update_step(5, f"Bước 5/6: Lồng tiếng AI ({'Giọng Chí Mai RVC' if v_source == 'rvc' else 'Giọng Hoài My'})...", percent=85)
        dubbing_audio_files = await generate_dubbing_audio(
            translated_segments, dubbing_dir, voice_source=v_source, voice_param=v_param
        )

        # ĐỒNG BỘ THỜI GIAN BIẾN MẤT CỦA PHỤ ĐỀ THEO GIỌNG ĐỌC
        for i, audio_info in enumerate(dubbing_audio_files):
            if audio_info:
                idx = audio_info.get("index")
                actual_duration = audio_info.get("actual_audio_duration", 0)
                for seg in translated_segments:
                    if getattr(seg, "index", None) == idx and actual_duration > 0:
                        new_end = seg.start + datetime.timedelta(seconds=actual_duration + 0.1)
                        seg.end = new_end
                        break

        # CHỐNG ĐÈ SUB (Anti-Overlap): Đảm bảo sub trước phải biến mất trước khi sub sau xuất hiện
        for i in range(len(translated_segments) - 1):
            if translated_segments[i].end > translated_segments[i+1].start:
                safe_end = translated_segments[i+1].start - datetime.timedelta(seconds=0.05)
                if safe_end > translated_segments[i].start:
                    translated_segments[i].end = safe_end
                else:
                    translated_segments[i].end = translated_segments[i].start + datetime.timedelta(seconds=0.1)

        # Căn chỉnh phụ đề ASS
        ass_path = os.path.join(out_dir, "final.ass")
        await asyncio.to_thread(generate_ass_file, translated_segments, [], ass_path, play_res_x=vid_w, play_res_y=vid_h, main_y_pct=main_y_pct)

        # Trộn nhạc nền sạch với giọng lồng tiếng
        await asyncio.to_thread(mix_audio_pydub, no_vocals_audio, dubbing_audio_files, mixed_audio, original_volume_db=-2, dubbing_volume_db=1)

        _raise_if_stopped()
        await notify("🎬 Bước 6/6: Đang Render video thành phẩm (Multi-threading)...")
        job_tracker.update_step(6, "Bước 6/6: Render video thành phẩm bằng NVENC GPU...", percent=95)
        res = await asyncio.to_thread(process_video, video_path, ass_path, mixed_audio, final_video, main_y_pct=main_y_pct, delogo=False)
        if not res or not os.path.exists(final_video):
            await notify("❌ Lỗi trong quá trình render video!")
            job_tracker.set_error(
                file_name, "Render video NVENC thất bại", fatal=False
            )
            return False

        _raise_if_stopped()
        # Lưu thành phẩm vào thư mục đầu ra
        probe = await asyncio.to_thread(probe_downloaded_video, final_video)
        if not probe.audio_stream_count:
            raise RuntimeError("Thành phẩm không có luồng âm thanh.")
        os.makedirs(output_dir, exist_ok=True)
        final_dest = os.path.join(output_dir, f"Dubbed_{base_name}.mp4")
        atomic_copy_file(final_video, final_dest)
        receipt_path = _receipt_path(final_dest)
        receipt_path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_json(receipt_path, {
            "input_sha256": fingerprint(video_path),
            "output_sha256": fingerprint(final_dest),
        })

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
        job_tracker.finish_video(file_name, final_dest, dt)
        await notify(f"✅ Hoàn thành video ({dt}s) -> Đã lưu vào {final_dest}")
        # Dọn dẹp thư mục tạm trong workspace để giải phóng dung lượng ổ C
        try:
            _cleanup_job_directory(out_dir)
        except Exception as cleanup_error:
            logger.warning("Không thể dọn thư mục tạm %s: %s", out_dir, cleanup_error)
        return True

    except BatchStopRequested as stop_error:
        logger.info("[%s] %s", file_name, stop_error)
        await notify(f"⏹️ {stop_error}")
        return False
    except Exception as e:
        logger.error(f"Lỗi xử lý file {file_name}: {e}", exc_info=True)
        job_tracker.set_error(file_name, str(e), fatal=False)
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
    job_id: str = None,
):
    """Own the complete lifecycle, including failures from callbacks and cancellation."""
    job_id = job_id or uuid.uuid4().hex
    worker = asyncio.create_task(_process_batch_folder(
        input_dir, output_dir, progress_callback, job_id))
    try:
        result = await asyncio.shield(worker)
        status = job_tracker.get_status()
        if status.get("job_id") == job_id and status.get("active"):
            job_tracker.finish_batch()
        return result
    except asyncio.CancelledError:
        # to_thread cannot be killed safely: keep ownership until it actually exits.
        shared_state.stop_requested = True
        if job_tracker.get_status().get("job_id") == job_id:
            job_tracker.request_stop()
        try:
            await worker
        finally:
            raise
    except job_tracker.JobAlreadyRunningError:
        raise
    except Exception as exc:
        job_tracker.fail_batch(str(exc), job_id=job_id)
        raise
    finally:
        job_tracker.release_batch(job_id)


async def _process_batch_folder(
    input_dir: str = DEFAULT_INPUT_DIR,
    output_dir: str = DEFAULT_OUTPUT_DIR,
    progress_callback=None,
    job_id: str = None,
):
    """
    Quét và xử lý toàn bộ video trong thư mục đầu vào
    """
    from batch_control import stop_check
    stop_check.set(_stop_requested)
    if not os.path.exists(input_dir):
        os.makedirs(input_dir, exist_ok=True)
        msg = f"📁 Đã tạo thư mục đầu vào: `{input_dir}`. Bạn hãy thả các video cần edit vào đây nhé!"
        logger.info(msg)
        if progress_callback:
            await progress_callback(msg)
        return

    os.makedirs(output_dir, exist_ok=True)
    video_files = sorted([
        os.path.join(input_dir, f) for f in os.listdir(input_dir)
        if f.lower().endswith(SUPPORTED_EXTENSIONS) and not f.startswith("Dubbed_")
        and os.path.isfile(os.path.join(input_dir, f))
    ])

    if not video_files:
        msg = f"⚠️ Không tìm thấy video nào trong `{input_dir}`. Hãy thả file video (.mp4, .mkv, .mov...) vào đây!"
        logger.info(msg)
        if progress_callback:
            await progress_callback(msg)
        return

    total = len(video_files)
    job_id = job_tracker.start_batch(total, input_dir, output_dir, job_id=job_id)
    shared_state.stop_requested = False
    start_msg = f"🚀 Bắt đầu xử lý hàng loạt **{total} video** từ thư mục:\n📂 `{input_dir}`\n💾 Đầu ra: `{output_dir}`"
    logger.info(start_msg)
    if progress_callback:
        await progress_callback(start_msg)

    # Thư mục lưu các video gốc đã xử lý xong
    processed_archive = os.path.join(input_dir, "processed")
    os.makedirs(processed_archive, exist_ok=True)

    success_count = 0
    failure_count = 0
    for idx, vpath in enumerate(video_files, 1):
        if _stop_requested():
            break
        vname = os.path.basename(vpath)
        base_stem = os.path.splitext(vname)[0]

        # Kiểm tra nếu video này đã được render thành phẩm trong output_dir thì bỏ qua
        expected_render = os.path.join(output_dir, f"Dubbed_{base_stem}.mp4")
        if os.path.isfile(expected_render) and await asyncio.to_thread(_verified_output, vpath, expected_render):
            skip_msg = f"⏩ [{idx}/{total}] Video `{vname}` đã có thành phẩm (`{os.path.basename(expected_render)}`). Bỏ qua..."
            logger.info(skip_msg)
            if progress_callback:
                await progress_callback(skip_msg)
            try:
                _archive_source(vpath, processed_archive)
            except Exception:
                pass
            continue

        if os.path.exists(expected_render):
            failure_count += 1
            job_tracker.set_error(vname, "Thành phẩm cũ chưa xác minh hoặc khác đầu vào; giữ nguyên để kiểm tra.", fatal=False)
            continue

        step_msg = f"🎬 **[{idx}/{total}] Đang xử lý:** `{vname}`..."
        logger.info(step_msg)
        if progress_callback:
            await progress_callback(step_msg)

        job_tracker.start_video(vname, idx, total)
        ok = await process_single_local_video(vpath, output_dir, progress_callback)
        if _stop_requested():
            break
        if not ok:
            failure_count += 1
        if ok:
            success_count += 1
            # Di chuyển file gốc đã làm xong sang thư mục processed để không bị trùng lặp
            try:
                _archive_source(vpath, processed_archive)
            except Exception as mv_err:
                logger.warning(f"Không thể di chuyển file gốc: {mv_err}")

    if _stop_requested():
        job_tracker.mark_stopped()
        stop_msg = f"⏹️ Đã dừng batch. Hoàn thành {success_count}/{total} video."
        logger.info(stop_msg)
        if progress_callback:
            await progress_callback(stop_msg)
        return

    if failure_count:
        job_tracker.fail_batch(f"{failure_count}/{total} video xử lý lỗi.", job_id=job_id)
    else:
        job_tracker.finish_batch()
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
