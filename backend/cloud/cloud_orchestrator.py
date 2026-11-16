"""
cloud_orchestrator.py - Bộ điều phối thực thi công việc Cloud GPU cho Tool V1.
Quản lý chu kỳ sống của session Colab: Tạo máy -> Upload -> Chạy Pipeline -> Tải về -> Huỷ máy.
"""

import logging
import os
import re
import shlex
import shutil
import tarfile
import tempfile
import time
import uuid
from pathlib import Path
from typing import Callable, Optional

from .cloud_config import DEFAULT_CONFIG
from .cloud_job_store import CloudJob, CloudJobStore
from .colab_bridge import (
    create_session,
    download_file,
    exec_cmd,
    get_usage,
    stop_session,
    upload_file,
)

logger = logging.getLogger("cloud.orchestrator")

GPU_CU_RATES = {
    "T4": 1.15,    # CU / giờ (NVIDIA T4 16GB)
    "L4": 1.94,    # CU / giờ (NVIDIA L4 24GB)
    "A100": 3.85,  # CU / giờ (NVIDIA A100 40GB)
    "H100": 8.00,  # CU / giờ (NVIDIA H100 80GB)
    "G4": 1.50,    # CU / giờ (Google Cloud G4)
    "V5E1": 2.00,  # CU / giờ (Google TPU v5e)
    "V6E1": 3.50,  # CU / giờ (Google TPU v6e Trillium)
    "CPU": 0.10,   # CU / giờ (Standard CPU)
}

class CloudJobCancelled(RuntimeError):
    pass


class CloudOrchestrator:
    def __init__(self, store: Optional[CloudJobStore] = None):
        self.store = store or CloudJobStore()
        self.backend_dir = Path(__file__).resolve().parent.parent
        self.gpu_scripts_dir = Path(__file__).resolve().parent / "gpu_scripts"

    def process_job(
        self,
        job: CloudJob,
        progress_callback: Optional[Callable[[int, str], None]] = None,
        stop_checker: Optional[Callable[[], bool]] = None,
    ) -> bool:
        """
        Thực thi trọn vẹn 1 công việc trên Colab GPU.
        LUÔN huỷ session Colab trong khối finally.
        """
        job_id = job.job_id
        clean_id = re.sub(r"[^a-zA-Z0-9]", "", job_id)[-8:] or uuid.uuid4().hex[:6]
        session_name = f"cjob{clean_id}"
        gpu_type = job.gpu_type.upper()
        rate_per_hour = GPU_CU_RATES.get(gpu_type, 1.15)
        start_time = time.time()
        session_attempted = False

        def _check_cancelled():
            if stop_checker and stop_checker():
                self.store.cancel_job(job_id)
            if self.store.is_cancelled(job_id):
                raise CloudJobCancelled("Cloud job đã bị hủy hoặc kết thúc.")

        def _remote(function, *args, **kwargs):
            _check_cancelled()
            result = function(*args, **kwargs)
            _check_cancelled()
            return result

        def _update(percent: int, step_name: str, **kwargs):
            _check_cancelled()
            if not self.store.update_progress(job_id, percent=percent, step_name=step_name, **kwargs):
                raise CloudJobCancelled("Cloud job không còn hoạt động.")
            if progress_callback:
                try:
                    progress_callback(percent, step_name)
                except Exception:
                    pass

        logger.info("=== BẮT ĐẦU XỬ LÝ CLOUD JOB %s (Video: %s, GPU: %s) ===", job_id, job.video_name, gpu_type)

        try:
            # 1. Kiểm tra số dư Compute Units trước khi chạy
            usage = _remote(get_usage)
            balance = usage.get("balance", 0.0)
            if usage.get("ok") and balance < 0.5:
                err = f"Số dư Colab không đủ (còn {balance} CU). Vui lòng nạp thêm CU."
                _update(0, "Lỗi: Hết Compute Units", state="error", error=err)
                return False

            # Kiểm tra hạn mức ngân sách CU trong ngày
            budget_info = self.store.get_cu_budget_info()
            if budget_info["is_exceeded"]:
                err = (
                    f"Đã chạm hạn mức CU trong ngày "
                    f"({budget_info['spent_today']:.2f} / {budget_info['budget_daily']:.2f} CU). "
                    f"Vui lòng cập nhật hạn mức trên Dashboard hoặc đợi sang ngày mới để bảo vệ tài khoản!"
                )
                logger.warning(err)
                _update(0, "Lỗi: Đã chạm hạn mức CU hôm nay", state="error", error=err)
                return False

            # 2. Khởi tạo Session Colab
            _update(
                10,
                f"Đang tạo máy ảo GPU {gpu_type} trên Colab...",
                state="creating_session",
                session_name=session_name,
            )
            self.store.start_billing(job_id, rate_per_hour)
            session_attempted = True
            created, msg = _remote(create_session, session_name, gpu=gpu_type)
            if not created:
                _update(
                    0,
                    "Lỗi: Tạo máy ảo Colab thất bại",
                    state="error",
                    error=f"Không thể tạo máy Colab: {msg}",
                )
                return False

            # 3. Upload mã nguồn và video lên VM
            is_url = job.video_path.startswith("http://") or job.video_path.startswith("https://")
            _update(
                20,
                "Đang đóng gói và tải mã nguồn AI lên máy ảo Colab...",
                state="uploading",
            )

            # Gom toàn bộ file script & wheel thành 1 file tar.gz duy nhất để upload 1 lần
            bundle_path = Path(tempfile.gettempdir()) / f"bundle_{session_name}.tar.gz"
            try:
                with tarfile.open(bundle_path, "w:gz") as tar:
                    backend_root = self.gpu_scripts_dir.parent.parent
                    v1_core_modules = [
                        backend_root / name
                        for name in (
                            "ass_utils.py",
                            "subtitle_layout.py",
                            "subtitle_text.py",
                            "v1_ocr_geometry.py",
                            "ocr_subtitle_locator.py",
                            "v1_timing_solver.py",
                            "v1_speech_guard.py",
                        )
                    ]
                    all_files = (
                        list(self.gpu_scripts_dir.glob("*.whl"))
                        + list(self.gpu_scripts_dir.glob("*.py"))
                        + [self.gpu_scripts_dir / "requirements_cloud.txt"]
                        + v1_core_modules
                    )
                    upload_targets = list({p.name: p for p in all_files if p.is_file()}.values())
                    for s_file in upload_targets:
                        tar.add(s_file, arcname=s_file.name)

                up_ok, up_msg = _remote(upload_file, session_name, bundle_path, "cloud_bundle.tar.gz")
                if not up_ok:
                    raise RuntimeError(f"Upload mã nguồn lên Colab thất bại: {up_msg}")

                # Giải nén bundle trên VM
                tar_ok, tar_msg = _remote(exec_cmd, session_name, "tar -xzf /content/cloud_bundle.tar.gz -C /content")
                if not tar_ok:
                    raise RuntimeError(f"Giải nén mã nguồn trên Colab thất bại: {tar_msg}")
            finally:
                bundle_path.unlink(missing_ok=True)

            if is_url:
                _update(
                    30,
                    f"Đã nhận link: {job.video_name}. Colab sẽ tải video trực tiếp trên cloud...",
                    state="uploading",
                )
            else:
                _update(
                    30,
                    f"Đang tải video {job.video_name} lên máy ảo...",
                    state="uploading",
                )
                v_ok, v_msg = _remote(upload_file, session_name, job.video_path)
                if not v_ok:
                    raise RuntimeError(f"Upload video thất bại: {v_msg}")

            # 4. Cài đặt môi trường trên VM
            _update(
                40,
                "Đang chuẩn bị môi trường AI (Demucs, Whisper Large-v3, CapCut TTS)...",
                state="processing",
            )
            env_ok, env_msg = _remote(exec_cmd, session_name, "cd /content && python3 setup_env.py", timeout=600)
            logger.info("Env setup output:\n%s", env_msg)
            if not env_ok:
                raise RuntimeError(f"Cài đặt môi trường AI trên Colab thất bại:\n{env_msg[-500:]}")

            # 5. Chạy Pipeline xử lý video chính
            _update(
                50,
                "Đang chạy lồng tiếng chuẩn V1 (Demucs tách âm, Whisper GPU, CapCut TTS, EBU R128)...",
                state="processing",
            )
            gemini_key = os.getenv("GEMINI_API_KEY", "").strip()
            voice_choice = getattr(job, "voice", "BV562_streaming") or "BV562_streaming"

            input_arg = job.video_path if is_url else f"/content/{Path(job.video_path).name}"
            exec_command = f"cd /content && python3 full_pipeline.py --input {shlex.quote(input_arg)} --voice {shlex.quote(voice_choice)}"
            if gemini_key:
                exec_command = f"export GEMINI_API_KEY={shlex.quote(gemini_key)} && {exec_command}"

            pipe_ok, pipe_output = _remote(exec_cmd, session_name, exec_command, timeout=DEFAULT_CONFIG["exec_timeout_seconds"])
            logger.info("Pipeline Colab output:\n%s", pipe_output)
            if not pipe_ok:
                raise RuntimeError(f"Pipeline Colab thất bại:\n{pipe_output[-500:]}")

            # 6. Tải video thành phẩm và kết quả về máy
            _update(
                85,
                "Đang tải video thành phẩm về máy tính...",
                state="downloading",
            )
            job_res_dir = Path(DEFAULT_CONFIG["results_dir"]) / job_id
            job_res_dir.mkdir(parents=True, exist_ok=True)

            dl_ok, dl_path = _remote(download_file, session_name, "output_video.mp4", job_res_dir)
            if not dl_ok:
                raise RuntimeError("Không tải được video thành phẩm từ Colab về.")

            # Tải thêm metadata và phụ đề nếu có
            _remote(download_file, session_name, "result_metadata.json", job_res_dir)
            _remote(download_file, session_name, "subtitles.srt", job_res_dir)
            _remote(download_file, session_name, "subtitles_vi.srt", job_res_dir)
            _remote(download_file, session_name, "subtitles_vi.ass", job_res_dir)

            _check_cancelled()
            stopped, stop_message = stop_session(session_name)
            if not stopped:
                raise RuntimeError("Chưa xác nhận dừng GPU: " + stop_message)
            self.store.finish_billing(job_id)
            session_attempted = False

            # 7. Di chuyển thành phẩm vào thư mục đầu ra D:\banve (hoặc cấu hình)
            out_env_dir = os.getenv("AUTODUB_OUTPUT_DIR", r"D:\banve")
            final_out_dir = Path(out_env_dir) if Path(out_env_dir).exists() else job_res_dir
            final_out_dir.mkdir(parents=True, exist_ok=True)

            final_video_path = final_out_dir / f"{DEFAULT_CONFIG['output_prefix']}{job.video_name}"
            _check_cancelled()
            staged = final_video_path.with_name(f".{final_video_path.name}.{job_id}.partial")
            try:
                shutil.copy2(dl_path, staged)
                _check_cancelled()
                if not self.store.publish_if_active(job_id, str(final_video_path),
                        lambda: os.replace(staged, final_video_path)):
                    raise CloudJobCancelled("Job bị hủy trước khi xuất bản video.")
            finally:
                staged.unlink(missing_ok=True)

            # 8. Tính lượng CU thực tế tiêu thụ
            elapsed_sec = time.time() - start_time
            cu_spent = round((elapsed_sec / 3600.0) * rate_per_hour, 3)

            logger.info("=== HOÀN THÀNH CLOUD JOB %s trong %.1fs (Tiêu thụ ~%.3f CU) ===", job_id, elapsed_sec, cu_spent)
            return True

        except CloudJobCancelled:
            logger.info("Cloud job %s đã hủy; không tiếp tục xử lý hoặc xuất bản.", job_id)
            return False
        except Exception as exc:
            logger.error("Lỗi trong quá trình xử lý Cloud Job %s: %s", job_id, exc, exc_info=True)
            self.store.update_progress(
                job_id, percent=0,
                step_name=f"Lỗi: {str(exc)[:120]}",
                state="error",
                error=str(exc),
            )
            return False

        finally:
            # BẮT BUỘC: Huỷ session để không bao giờ bị rò rỉ CU!
            if session_attempted:
                logger.info("Dọn dẹp: Dừng session Colab %s...", session_name)
                try:
                    stopped, message = stop_session(session_name)
                except Exception as exc:
                    stopped, message = False, str(exc)
                if stopped:
                    self.store.finish_billing(job_id)
                else:
                    self.store.note_cleanup_failure(job_id, message)
                    logger.error("Chưa xác nhận dừng session %s: %s", session_name, message)
                    return False
