"""Run complete offline end-to-end verification job on the 13m20s Douyin video.

This script executes the entire Tool V2 pipeline (version 2.13.0) into an isolated
benchmark directory (workspace/benchmark_runs/e2e_verify_job) without touching Tool V1
and without delivering to user production folders or sending Telegram notifications.
"""

from __future__ import annotations

import asyncio
import datetime
import json
import logging
import os
import shutil
import sys
import time
from pathlib import Path
from typing import Any, Dict, Optional

# UTF-8 encoding for Windows console with write_through for instant flush
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True, write_through=True)
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace", line_buffering=True, write_through=True)

BASE_DIR = Path(__file__).resolve().parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

import argparse

# Load .env configuration
env_file = BASE_DIR / ".env"
if env_file.is_file():
    with open(env_file, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if "=" in line and not line.startswith("#"):
                k, v = line.split("=", 1)
                os.environ[k.strip()] = v.strip().strip('"').strip("'")

parser = argparse.ArgumentParser(description="Run offline verification benchmark for Tool V2")
parser.add_argument("--cold", action="store_true", default=True, help="Run as fresh cold benchmark in e2e_cold_verify_job")
parser.add_argument("--warm", action="store_true", help="Run warm benchmark in e2e_verify_job")
parser.add_argument("--benchmark-dir", type=str, default=None, help="Custom benchmark output directory")
parser.add_argument("--skip-clean", action="store_true", help="Do not wipe job directory before running")
cli_args, _ = parser.parse_known_args()

if cli_args.benchmark_dir:
    benchmark_dir = Path(cli_args.benchmark_dir)
elif cli_args.warm:
    benchmark_dir = BASE_DIR.parent / "workspace" / "benchmark_runs" / "e2e_verify_job"
else:
    benchmark_dir = BASE_DIR.parent / "workspace" / "benchmark_runs" / "e2e_cold_verify_job"

benchmark_log_file = benchmark_dir / "verification_run.log"
benchmark_log_file.parent.mkdir(parents=True, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(benchmark_log_file, encoding="utf-8"),
    ],
    force=True,
)
logger = logging.getLogger("offline_verification")

from pipeline_v2.config import PipelineSettings
from pipeline_v2.video_pipeline import (
    VideoPipelineRequest,
    VideoPipelineRunner,
    discover_rvc_model,
)


def extract_targeted_qc_frames(
    video_path: Path,
    output_dir: Path,
    timestamps: list[float],
) -> list[str]:
    """Extract frames at exact timestamps to verify no subtitle leaks and no packaging coverage."""
    import cv2

    output_dir.mkdir(parents=True, exist_ok=True)
    cap = cv2.VideoCapture(str(video_path))
    if not cap.isOpened():
        logger.error(f"Cannot open video for frame extraction: {video_path}")
        return []

    saved_paths = []
    fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
    total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)

    for t in timestamps:
        frame_idx = int(round(t * fps))
        if frame_idx < 0 or frame_idx >= total_frames:
            continue
        cap.set(cv2.CAP_PROP_POS_MSEC, t * 1000.0)
        ret, frame = cap.read()
        if ret and frame is not None:
            filename = f"verify_t{t:07.2f}s.jpg"
            out_file = output_dir / filename
            cv2.imwrite(str(out_file), frame, [int(cv2.IMWRITE_JPEG_QUALITY), 95])
            saved_paths.append(str(out_file))

    cap.release()
    return saved_paths


async def main():
    source_video = Path(r"C:\tool v2\workspace\downloads\1789932323_c26d2025_douyin_7682719002690374963.mp4")
    if not source_video.is_file():
        logger.error(f"Source video not found: {source_video}")
        sys.exit(1)

    job_dir = benchmark_dir / "job"
    output_dir = benchmark_dir / "output"
    output_dir.mkdir(parents=True, exist_ok=True)
    final_output_path = output_dir / "verified_douyin_13m20s.mp4"

    # Enforce fresh cold benchmark run when requested
    if not cli_args.skip_clean and not cli_args.warm:
        if job_dir.exists():
            logger.info(f"Cleaning existing job directory for 100% cold run: {job_dir}")
            shutil.rmtree(str(job_dir), ignore_errors=True)

    pipeline_v2_dir = job_dir / "pipeline_v2"
    manifest_file = pipeline_v2_dir / "job_manifest.json"

    # RVC Model discovery
    rvc_model = discover_rvc_model(Path(r"C:\tool v2\MyVoiceModel_v2"))
    if not rvc_model:
        rvc_model = discover_rvc_model(Path(r"C:\tool v2\workspace"))

    settings = PipelineSettings.from_env()

    logger.info("=" * 80)
    mode_str = "WARM BENCHMARK" if cli_args.warm else "100% COLD BENCHMARK (NO CACHE)"
    logger.info(f"OFFLINE END-TO-END VERIFICATION RUN — PIPELINE V2.13.2 [{mode_str}]")
    logger.info("=" * 80)
    logger.info(f"Source: {source_video} ({source_video.stat().st_size / (1024*1024):.1f} MB)")
    logger.info(f"Benchmark Dir: {benchmark_dir}")
    logger.info(f"RVC Model: {rvc_model}")
    logger.info(f"Settings: QC_GATE_POLICY={settings.qc_gate_policy}, "
                f"RVC_BATCH_SEGMENTS={settings.rvc_batch_segments}, "
                f"TTS_BATCH_SEGMENTS={settings.tts_batch_segments}, "
                f"ENABLE_ADAPTIVE_OCR={settings.enable_adaptive_ocr}")
    logger.info("=" * 80)

    start_wall_time = time.time()
    stage_durations: Dict[str, float] = {}

    def on_progress(stage: str, state: str):
        elapsed = int(time.time() - start_wall_time)
        m, s = divmod(elapsed, 60)
        logger.info(f"⏱️ [{m:02d}:{s:02d}] Stage: {stage} -> {state}")

    async def async_progress(stage: str, state: str):
        on_progress(stage, state)

    request = VideoPipelineRequest(
        video_path=source_video,
        job_directory=job_dir,
        output_path=final_output_path,
        delivery_copy_path=None,  # Offline verification only; do not write to D:\banve
        settings=settings,
        api_key=os.getenv("GEMINI_API_KEY", ""),
        voice_source="rvc" if rvc_model else "edge",
        voice_param=str(rvc_model) if rvc_model else "vi-VN-HoaiMyNeural",
        rvc_model_path=rvc_model,
        progress=async_progress,
    )

    try:
        runner = VideoPipelineRunner(request)
        result = await runner.run()
    except Exception as exc:
        logger.error(f"Pipeline run raised exception: {exc}", exc_info=True)
        raise

    total_wall_time = time.time() - start_wall_time
    total_m, total_s = divmod(int(total_wall_time), 60)

    logger.info("=" * 80)
    logger.info(f"PIPELINE RUN COMPLETED IN {total_m}m {total_s}s (Total: {total_wall_time:.1f}s)")
    logger.info(f"Final Output: {final_output_path} (Size: {final_output_path.stat().st_size / (1024*1024):.1f} MB)")
    logger.info(f"QC Allowed: {result.qc_allowed}")
    logger.info(f"QC Report: {result.qc_report_path}")
    logger.info("=" * 80)

    # Parse and report exact manifest stage timings
    manifest_stages = {}
    if manifest_file.is_file():
        manifest_data = json.loads(manifest_file.read_text(encoding="utf-8"))
        stages_data = manifest_data.get("stages", {})
        for name, info in stages_data.items():
            started = info.get("started_at")
            finished = info.get("finished_at")
            if started and finished:
                try:
                    t_start = datetime.datetime.fromisoformat(started.replace("Z", "+00:00"))
                    t_finish = datetime.datetime.fromisoformat(finished.replace("Z", "+00:00"))
                    duration = (t_finish - t_start).total_seconds()
                    manifest_stages[name] = round(duration, 2)
                except Exception:
                    pass

    # Read QC Report
    qc_metrics = {}
    if result.qc_report_path and Path(result.qc_report_path).is_file():
        qc_metrics = json.loads(Path(result.qc_report_path).read_text(encoding="utf-8"))

    # Targeted frame extraction
    # Sample timestamps at: 13.5s, 34.2s, 100.0s, 250.0s, 400.0s, 600.0s, 750.0s
    sample_timestamps = [10.0, 13.5, 34.2, 50.0, 101.0, 205.0, 315.0, 450.0, 600.0, 750.0]
    extracted_frames = extract_targeted_qc_frames(
        final_output_path,
        benchmark_dir / "visual_verification",
        sample_timestamps,
    )

    # Segment count from timed segments
    segment_count = 0
    timed_seg_file = pipeline_v2_dir / "artifacts" / "timed_segments.json"
    if timed_seg_file.is_file():
        try:
            segs = json.loads(timed_seg_file.read_text(encoding="utf-8"))
            segment_count = len(segs)
        except Exception:
            pass

    summary = {
        "status": "success" if result.qc_allowed else "qc_failed",
        "pipeline_version": "2.13.2",
        "benchmark_mode": "warm" if cli_args.warm else "cold",
        "segment_count": segment_count,
        "total_wall_time_seconds": round(total_wall_time, 2),
        "total_wall_time_formatted": f"{total_m}m {total_s}s",
        "stage_durations_seconds": manifest_stages,
        "qc_metrics": qc_metrics,
        "extracted_qc_frames": extracted_frames,
        "final_video_path": str(final_output_path),
        "final_video_size_bytes": final_output_path.stat().st_size if final_output_path.is_file() else 0,
    }

    summary_file = benchmark_dir / "verification_summary.json"
    summary_file.write_text(json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8")
    logger.info(f"Summary saved to: {summary_file}")
    print(json.dumps(summary, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    asyncio.run(main())
