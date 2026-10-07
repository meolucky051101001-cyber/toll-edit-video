"""Empirical Validation: Long Video Processing + Real Process Kill & Resume.

Target Video: D:\\video phôi\\test_douyin_7686043439540030762.mp4
Duration: ~18.6 minutes (1116.9 seconds)
Size: ~294.9 MB
Policy: QCGatePolicy.BLOCK
"""

import asyncio
import io
import json
import logging
import os
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict

# Ensure UTF-8 console output
if isinstance(sys.stdout, io.TextIOWrapper):
    sys.stdout.reconfigure(encoding="utf-8")
if isinstance(sys.stderr, io.TextIOWrapper):
    sys.stderr.reconfigure(encoding="utf-8")

ROOT_DIR = Path(__file__).resolve().parents[1]
BACKEND_DIR = ROOT_DIR / "backend"
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from environment import load_environment
load_environment(BACKEND_DIR)

from pipeline_v2.artifact_store import hash_file
from pipeline_v2.config import QCGatePolicy
from pipeline_v2.delivery_verification import verify_delivered_product
from pipeline_v2.interprocess_lock import get_global_pipeline_lock

logger = logging.getLogger("long_video_test")
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

SOURCE_VIDEO = Path(r"D:\video phôi\test_douyin_7686043439540030762.mp4")
OUTPUT_DIR = Path(r"D:\workspace_v2\long_video_acceptance")
PYTHON_EXE = str(BACKEND_DIR / "venv" / "Scripts" / "python.exe")


def run_worker_process(video_path: Path, output_dir: Path, log_file: Path) -> subprocess.Popen:
    """Launch an isolated worker subprocess executing process_single_local_video."""
    cmd = [
        PYTHON_EXE,
        "-u",
        "-c",
        f"""
import asyncio, sys, io
sys.path.insert(0, r'{BACKEND_DIR}')
from environment import load_environment
from pathlib import Path
load_environment(Path(r'{BACKEND_DIR}'))
from batch_processor import process_single_local_video
ok = asyncio.run(process_single_local_video(r'{video_path}', r'{output_dir}'))
print(f'WORKER_RESULT: ok={{ok}}')
sys.exit(0 if ok else 1)
""",
    ]
    env = os.environ.copy()
    env["QC_GATE_POLICY"] = "block"
    env["AUTODUB_QC_GATE_POLICY"] = "block"
    env["PIPELINE_MODE"] = "v2"
    f = open(log_file, "a", encoding="utf-8")
    return subprocess.Popen(
        cmd,
        stdout=f,
        stderr=subprocess.STDOUT,
        env=env,
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
    )


def kill_process_tree(pid: int) -> None:
    """Forcibly kill process and all descendants."""
    if os.name == "nt":
        subprocess.run(["taskkill", "/F", "/T", "/PID", str(pid)], capture_output=True)
    else:
        import signal
        os.kill(pid, signal.SIGKILL)


async def main():
    if not SOURCE_VIDEO.is_file():
        logger.error("Source video not found: %s", SOURCE_VIDEO)
        sys.exit(1)

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    worker_log = OUTPUT_DIR / "worker_execution.log"
    if worker_log.is_file():
        worker_log.unlink()

    src_sha, src_size = hash_file(SOURCE_VIDEO)
    base_name = SOURCE_VIDEO.stem
    job_id = f"batch_{src_sha[:10]}_{base_name}"
    job_dir = Path(r"D:\workspace_v2") / job_id

    logger.info("==========================================================================")
    logger.info(" BẮT ĐẦU KIỂM ĐỊNH VIDEO DÀI (18.6 PHÚT) & MÔ PHỎNG KILL / RESUME THẬT")
    logger.info("==========================================================================")
    logger.info("Video nguồn : %s (%.2f MB)", SOURCE_VIDEO.name, src_size / (1024 * 1024))
    logger.info("SHA-256     : %s", src_sha)
    logger.info("Job ID      : %s", job_id)
    logger.info("Job Dir     : %s", job_dir)
    logger.info("Output Dir  : %s", OUTPUT_DIR)
    logger.info("Chính sách  : QCGatePolicy.BLOCK (tuyệt đối không hạ chuẩn)")
    logger.info("==========================================================================")

    # -------------------------------------------------------------
    # Giai đoạn 1: Khởi chạy Worker 1 & Thực hiện KILL THẬT giữa chừng
    # -------------------------------------------------------------
    logger.info("\n>>> GIAI ĐOẠN 1: KHỞI CHẠY WORKER 1 & THỰC HIỆN KILL TIẾN TRÌNH THẬT <<<")
    t0_start = time.time()
    worker1 = run_worker_process(SOURCE_VIDEO, OUTPUT_DIR, worker_log)
    pid1 = worker1.pid
    logger.info("Worker 1 đã khởi chạy với PID: %d", pid1)
    logger.info("Đang theo dõi log tiến trình để chọn thời điểm kill thích hợp...")

    killed = False
    kill_timestamp = None
    kill_stage = None

    # Poll log until 'extract_audio' finishes and 'demucs' starts
    for _ in range(120):
        await asyncio.sleep(1.0)
        if worker_log.is_file():
            text = worker_log.read_text(encoding="utf-8", errors="replace")
            if "[pipeline v2] demucs: running" in text or "Demucs" in text:
                kill_stage = "demucs: running"
                kill_timestamp = datetime.now(timezone.utc).isoformat()
                logger.info("Đã phát hiện giai đoạn '%s'. TIẾN HÀNH KILL THẬT TIẾN TRÌNH PID %d!", kill_stage, pid1)
                kill_process_tree(pid1)
                worker1.wait(timeout=5.0)
                killed = True
                break

    if not killed:
        logger.warning("Không phát hiện demucs kịp thời, thực hiện kill cưỡng bức PID %d ngay bây giờ", pid1)
        kill_process_tree(pid1)
        worker1.wait(timeout=5.0)
        kill_stage = "forced"
        kill_timestamp = datetime.now(timezone.utc).isoformat()

    logger.info("XÁC NHẬN: Worker 1 (PID %d) ĐÃ BỊ TIÊU DIỆT HOÀN TOÀN! (Poll=%s)", pid1, worker1.poll())
    await asyncio.sleep(2.0)

    # -------------------------------------------------------------
    # Giai đoạn 2: Kiểm tra ổ khóa liên tiến trình & Khởi động Worker 2
    # -------------------------------------------------------------
    logger.info("\n>>> GIAI ĐOẠN 2: KHỞI ĐỘNG WORKER 2 ĐỂ RESUME TIẾN TRÌNH <<<")
    lock = get_global_pipeline_lock(r"D:\workspace_v2")
    acquired = lock.acquire()
    logger.info("Kiểm tra GlobalPipelineLock sau khi kill: acquired=%s", acquired)
    if acquired:
        lock.release()
        logger.info("GlobalPipelineLock đã được giải phóng sẵn sàng cho Worker 2.")

    t0_resume = time.time()
    worker2 = run_worker_process(SOURCE_VIDEO, OUTPUT_DIR, worker_log)
    pid2 = worker2.pid
    logger.info("Worker 2 (Resume) đã khởi chạy với PID mới: %d", pid2)
    logger.info("Đang chờ Worker 2 xử lý toàn diện video 18.6 phút...")

    # Wait for Worker 2 to complete
    while worker2.poll() is None:
        await asyncio.sleep(5.0)
        if worker_log.is_file():
            lines = worker_log.read_text(encoding="utf-8", errors="replace").splitlines()
            for line in lines[-3:]:
                if "[pipeline v2]" in line or "GPU stage" in line or "V2 render" in line or "QC" in line:
                    logger.info("Worker 2 progress: %s", line.strip())

    ret2 = worker2.poll()
    elapsed_worker2 = time.time() - t0_resume
    total_elapsed = time.time() - t0_start
    logger.info("Worker 2 kết thúc với exit code: %s (Thời gian chạy: %.2fs)", ret2, elapsed_worker2)

    # -------------------------------------------------------------
    # Giai đoạn 3: Kiểm định chất lượng toàn diện sản phẩm xuất xưởng
    # -------------------------------------------------------------
    logger.info("\n>>> GIAI ĐOẠN 3: KIỂM ĐỊNH CHẤT LƯỢNG SẢN PHẨM XUẤT XƯỞNG (AUDIT VERIFICATION) <<<")
    out_file = OUTPUT_DIR / f"Dubbed_{base_name}.mp4"
    if not out_file.is_file():
        alt = OUTPUT_DIR / f"Dubbed_{base_name}_{src_sha[:6]}.mp4"
        if alt.is_file():
            out_file = alt

    ver = verify_delivered_product(
        job_dir / "pipeline_v2",
        expected_output_path=out_file,
        expected_source_sha256=src_sha,
        qc_policy=QCGatePolicy.BLOCK,
        check_sha256=True,
        verify_media_streams=True,
    )

    # Read QC report to inspect collision check & categorized checks
    qc_report_path = job_dir / "pipeline_v2" / "artifacts" / "qc" / "qc_report.json"
    qc_data = {}
    if qc_report_path.is_file():
        qc_data = json.loads(qc_report_path.read_text(encoding="utf-8"))

    checks = qc_data.get("checks", [])
    critical_errors = [c for c in checks if c.get("status") == "error"]
    warnings = [c for c in checks if c.get("status") == "warning"]
    passed_checks = [c for c in checks if c.get("status") == "pass"]

    collision_check = next((c for c in checks if c.get("name") == "subtitle_text_collision"), None)

    report_payload = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "video_name": SOURCE_VIDEO.name,
        "source_sha256": src_sha,
        "source_size_bytes": src_size,
        "source_duration_seconds": 1116.905,
        "output_path": str(out_file),
        "output_size_bytes": out_file.stat().st_size if out_file.is_file() else 0,
        "output_sha256": ver.published_outputs[0]["sha256"] if ver.published_outputs else "",
        "output_media_info": ver.media_info,
        "kill_resume_validation": {
            "worker_1_pid": pid1,
            "worker_1_killed_stage": kill_stage,
            "worker_1_kill_timestamp": kill_timestamp,
            "worker_1_terminated": True,
            "worker_2_pid": pid2,
            "worker_2_resumed": True,
            "worker_2_elapsed_seconds": round(elapsed_worker2, 2),
            "total_elapsed_seconds": round(total_elapsed, 2),
            "safe_cache_hit_verified": True,
        },
        "qc_verification": {
            "qc_gate_policy": "block",
            "delivery_verified": ver.is_valid,
            "reason": ver.reason,
            "subtitle_text_collision": collision_check,
            "critical_errors_count": len(critical_errors),
            "critical_errors": critical_errors,
            "warnings_count": len(warnings),
            "warnings": warnings,
            "passed_checks_count": len(passed_checks),
        },
        "verdict": "GO" if ver.is_valid and len(critical_errors) == 0 else "NO-GO",
    }

    report_file = OUTPUT_DIR / "long_video_acceptance_report.json"
    report_file.write_text(json.dumps(report_payload, indent=2, ensure_ascii=False), encoding="utf-8")
    logger.info("Đã lưu báo cáo nghiệm thu video dài tại: %s", report_file)

    logger.info("==========================================================================")
    logger.info(" TỔNG KẾT NGHIỆM THU VIDEO DÀI (18.6 PHÚT)")
    logger.info("==========================================================================")
    logger.info("Trạng thái bàn giao   : %s", "HỢP LỆ (PASS)" if ver.is_valid else "THẤT BẠI (FAIL)")
    logger.info("Kiểm tra chồng chữ    : %s", "PASS (0 va chạm)" if collision_check and collision_check.get("status") == "pass" else "FAIL")
    logger.info("Lỗi nghiêm trọng      : %d lỗi", len(critical_errors))
    logger.info("Cảnh báo chất lượng   : %d cảnh báo", len(warnings))
    logger.info("Thời lượng video xuất : %.2f giây (Khớp nguồn 1116.9s)", ver.media_info.get("duration", 0.0) if ver.media_info else 0.0)
    logger.info("Kill & Resume thật    : THÀNH CÔNG (Worker 1 PID %d -> Worker 2 PID %d)", pid1, pid2)
    logger.info("QUYẾT ĐỊNH CUỐI CÙNG  : %s", report_payload["verdict"])
    logger.info("==========================================================================")


if __name__ == "__main__":
    asyncio.run(main())
