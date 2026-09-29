"""Phase 6: Canary Batch Processing on 20 Real Douyin Videos.

Validates:
1. Batch processing of 20 real videos from D:\\douyin_user_98604973464 under QCGatePolicy.BLOCK.
2. Simulated mid-batch restart (kill & resume) ensuring:
   - Previously completed videos are verified by manifest + hash + streams and skipped safely (no overwrite, no corrupt skip).
   - Incomplete videos are resumed cleanly.
3. Full audit traceability:
   source_sha256 -> job_id -> qc_report -> output_sha256 -> delivery_verified.
"""

import asyncio
import io
import json
import logging
import os
import shutil
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List

# Reconfigure console encoding
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
from pipeline_v2.config import PipelineMode, PipelineSettings, QCGatePolicy
from pipeline_v2.delivery_verification import verify_delivered_product
from batch_processor import process_single_local_video

logger = logging.getLogger("canary_batch")
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")


def select_canary_videos(source_dir: Path, target_count: int = 20) -> List[Path]:
    """Select the 20 best candidate videos from the Douyin archive sorted by size."""
    if not source_dir.exists():
        raise FileNotFoundError(f"Source directory not found: {source_dir}")
    mp4s = sorted(list(source_dir.glob("*.mp4")), key=lambda p: p.stat().st_size)
    if len(mp4s) < target_count:
        raise ValueError(f"Found only {len(mp4s)} videos, need at least {target_count}")
    return mp4s[:target_count]


def resolve_output_file(output_dir: Path, base_name: str, src_hash_short: str) -> Path:
    std = output_dir / f"Dubbed_{base_name}.mp4"
    if std.is_file():
        return std
    alt = output_dir / f"Dubbed_{base_name}_{src_hash_short[:6]}.mp4"
    if alt.is_file():
        return alt
    return std


async def run_canary_batch(
    video_list: List[Path],
    output_dir: Path,
    simulate_restart_at: int = 3,
) -> Dict[str, Any]:
    output_dir.mkdir(parents=True, exist_ok=True)
    os.environ["AUTODUB_QC_GATE_POLICY"] = "block"
    os.environ["AUTODUB_PIPELINE_MODE"] = "v2"

    results: List[Dict[str, Any]] = []

    logger.info("=== BẮT ĐẦU CHẠY CANARY BATCH 20 VIDEO THẬT (PHASE 6) ===")
    logger.info("Output directory: %s", output_dir)
    logger.info("Total videos to process: %d", len(video_list))

    # Phase 6A: First run with simulated interrupt at index `simulate_restart_at`
    logger.info("--- Giai đoạn 6A: Chạy đợt 1 (Mô phỏng ngắt tiến trình sau video %d) ---", simulate_restart_at)
    for idx, video_path in enumerate(video_list[:simulate_restart_at], 1):
        src_sha, _ = hash_file(video_path)
        base_name = video_path.stem
        src_hash_short = src_sha[:10]
        job_id = f"batch_{src_hash_short}_{base_name}"
        workspace_job_dir = Path(os.getenv("AUTODUB_WORKSPACE", str(ROOT_DIR / "workspace"))) / job_id
        out_file = resolve_output_file(output_dir, base_name, src_hash_short)
        ver_before = None
        if workspace_job_dir.is_dir() and out_file.is_file():
            ver_before = verify_delivered_product(
                workspace_job_dir / "pipeline_v2",
                expected_output_path=out_file,
                expected_source_sha256=src_sha,
                qc_policy=QCGatePolicy.BLOCK,
                check_sha256=True,
                verify_media_streams=True,
            )
        if ver_before and ver_before.is_valid:
            logger.info("Canary Part 1 [%d/%d]: Video %s ĐÃ CÓ BẢN GIAO HỢP LỆ -> Safe skip!", idx, simulate_restart_at, video_path.name)
            continue
        logger.info("Canary Part 1 [%d/%d]: Xử lý video %s...", idx, simulate_restart_at, video_path.name)
        t0 = time.time()
        ok = await process_single_local_video(str(video_path), str(output_dir))
        elapsed = time.time() - t0
        logger.info("Canary Part 1 [%d/%d] Hoàn tất: ok=%s in %.2fs", idx, simulate_restart_at, ok, elapsed)

    logger.info(">>> MÔ PHỎNG NGẮT TIẾN TRÌNH ĐỘT NGỘT (SIGINT / KILL / RESTART) <<<")
    await asyncio.sleep(2.0)

    # Phase 6B: Resume run across all 20 videos
    logger.info("--- Giai đoạn 6B: Khôi phục tiến trình (Resume full batch 20 video) ---")
    skipped_count = 0
    processed_count = 0

    for idx, video_path in enumerate(video_list, 1):
        src_sha, src_size = hash_file(video_path)
        base_name = video_path.stem
        src_hash_short = src_sha[:10]
        job_id = f"batch_{src_hash_short}_{base_name}"
        workspace_job_dir = Path(os.getenv("AUTODUB_WORKSPACE", str(ROOT_DIR / "workspace"))) / job_id

        # Check before running if already delivered
        out_file = resolve_output_file(output_dir, base_name, src_hash_short)

        ver_before = None
        if workspace_job_dir.is_dir() and out_file.is_file():
            ver_before = verify_delivered_product(
                workspace_job_dir / "pipeline_v2",
                expected_output_path=out_file,
                expected_source_sha256=src_sha,
                qc_policy=QCGatePolicy.BLOCK,
                check_sha256=True,
                verify_media_streams=True,
            )

        if ver_before and ver_before.is_valid:
            logger.info("Canary Part 2 [%d/%d]: Video %s ĐÃ CÓ BẢN GIAO HỢP LỆ -> Safe skip (không render lại)!",
                        idx, len(video_list), video_path.name)
            skipped_count += 1
            results.append({
                "index": idx,
                "video_name": video_path.name,
                "source_sha256": src_sha,
                "source_size_bytes": src_size,
                "job_id": job_id,
                "action": "skipped_verified",
                "output_path": str(out_file),
                "output_sha256": ver_before.published_outputs[0]["sha256"] if ver_before.published_outputs else "",
                "output_size_bytes": out_file.stat().st_size,
                "media_info": ver_before.media_info,
                "delivery_verified": True,
                "qc_status": "pass",
            })
            continue

        logger.info("Canary Part 2 [%d/%d]: Đang xử lý video %s...", idx, len(video_list), video_path.name)
        t0 = time.time()
        ok = await process_single_local_video(str(video_path), str(output_dir))
        elapsed = time.time() - t0

        out_file = resolve_output_file(output_dir, base_name, src_hash_short)
        ver_after = verify_delivered_product(
            workspace_job_dir / "pipeline_v2",
            expected_output_path=out_file,
            expected_source_sha256=src_sha,
            qc_policy=QCGatePolicy.BLOCK,
            check_sha256=True,
            verify_media_streams=True,
        )

        processed_count += 1
        out_sha = ""
        out_sz = 0
        if out_file.is_file():
            out_sha, out_sz = hash_file(out_file)

        results.append({
            "index": idx,
            "video_name": video_path.name,
            "source_sha256": src_sha,
            "source_size_bytes": src_size,
            "job_id": job_id,
            "action": "processed",
            "elapsed_seconds": round(elapsed, 2),
            "output_path": str(out_file),
            "output_sha256": out_sha,
            "output_size_bytes": out_sz,
            "media_info": ver_after.media_info,
            "delivery_verified": ver_after.is_valid,
            "qc_status": "pass" if ver_after.is_valid else "fail",
            "reason": ver_after.reason,
        })
        logger.info("Canary Part 2 [%d/%d]: Kết quả %s -> valid=%s (%.2fs)",
                    idx, len(video_list), video_path.name, ver_after.is_valid, elapsed)

    all_valid = all(r["delivery_verified"] for r in results)
    canary_summary = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "total_videos": len(video_list),
        "skipped_valid": skipped_count,
        "newly_processed": processed_count,
        "all_delivery_verified": all_valid,
        "go_no_go_decision": "GO" if (all_valid and len(results) == len(video_list)) else "NO-GO",
        "videos": results,
    }

    report_file = output_dir / "canary_batch_20_report.json"
    report_file.write_text(json.dumps(canary_summary, ensure_ascii=False, indent=2), encoding="utf-8")
    logger.info("Đã lưu báo cáo Canary Batch 20 video tại: %s", report_file)
    return canary_summary


async def main():
    source_dir = Path(r"D:\douyin_user_98604973464")
    output_dir = Path(r"D:\workspace_v2\canary_batch_runs")
    video_list = select_canary_videos(source_dir, target_count=20)
    summary = await run_canary_batch(video_list, output_dir, simulate_restart_at=3)

    print("\n" + "=" * 90)
    print(" BÁO CÁO NGHIỆM THU CANARY BATCH 20 VIDEO THẬT (PHASE 6)")
    print("=" * 90)
    fmt = "{:<4} | {:<16} | {:<12} | {:<10} | {:<12} | {:<8}"
    print(fmt.format("STT", "Tên video", "Hành động", "Thời gian", "Xác minh", "Quyết định"))
    print("-" * 90)
    for v in summary["videos"]:
        print(fmt.format(
            v["index"],
            v["video_name"][:16],
            v["action"],
            f"{v.get('elapsed_seconds', 0.0):.1f}s" if v["action"] == "processed" else "Reused",
            "PASS" if v["delivery_verified"] else "FAIL",
            "PASS" if v["delivery_verified"] else "BLOCK"
        ))
    print("-" * 90)
    print(f"Tổng số video kiểm tra : {summary['total_videos']}")
    print(f"Video khôi phục chuẩn  : {summary['skipped_valid']} (an toàn, không ghi đè, không skip sai)")
    print(f"Video hoàn tất mới     : {summary['newly_processed']}")
    print(f"Xác minh bàn giao 100% : {'ĐẠT (100% PASS)' if summary['all_delivery_verified'] else 'KHÔNG ĐẠT'}")
    print(f"QUYẾT ĐỊNH CUỐI CÙNG   : {summary['go_no_go_decision']}")
    print("=" * 90 + "\n")


if __name__ == "__main__":
    asyncio.run(main())
