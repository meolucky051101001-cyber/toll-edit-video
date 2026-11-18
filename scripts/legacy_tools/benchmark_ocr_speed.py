"""Offline OCR benchmark comparing dense vs adaptive 2-phase OCR on reference video."""
import json
import logging
import os
import sys
import time
from pathlib import Path

# Setup UTF-8 and path
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger("ocr_benchmark")

BASE_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE_DIR))

from pipeline_v2.segments import segments_from_dicts
from ocr_utils import perform_video_ocr, release_ocr_reader


def run_benchmark(max_segments=None):
    video_path = Path(r"C:\tool v2\workspace\downloads\1789932323_c26d2025_douyin_7682719002690374963.mp4")
    if not video_path.exists():
        logger.error("Video file not found: %s", video_path)
        return

    trans_file = Path(r"C:\tool v2\workspace\job_douyin_7682719002690374963\pipeline_v2\artifacts\transcript\segments.json")
    with open(trans_file, "r", encoding="utf-8") as f:
        seg_dicts = json.load(f)["segments"]

    if max_segments:
        seg_dicts = seg_dicts[:max_segments]
        logger.info("Running benchmark on first %d segments", max_segments)
    else:
        logger.info("Running benchmark on all %d segments", len(seg_dicts))

    out_dir = Path(r"C:\tool v2\workspace\benchmark_runs\ocr_bench_douyin")
    out_dir.mkdir(parents=True, exist_ok=True)

    # 1. Benchmark Adaptive 2-Phase OCR
    logger.info("\n=== STARTING ADAPTIVE 2-PHASE OCR ===")
    segs_adaptive = segments_from_dicts(seg_dicts)
    metrics_adaptive = {}
    t0 = time.monotonic()
    try:
        blocks_a, w_a, h_a, y_a = perform_video_ocr(
            str(video_path),
            "vi",
            1.0,
            None,
            segs_adaptive,
            adaptive=True,
            metrics=metrics_adaptive,
        )
    finally:
        release_ocr_reader()
    adaptive_dur = time.monotonic() - t0

    logger.info("Adaptive OCR finished in %.2fs (%.2f min)", adaptive_dur, adaptive_dur / 60)
    logger.info("Adaptive metrics: %s", metrics_adaptive)
    logger.info("Adaptive main_y_pct: %.4f", y_a)

    sub_segs_a = sum(1 for s in segs_adaptive if getattr(s, "is_subtitle", False))
    logger.info("Adaptive detected subtitles for %d / %d segments", sub_segs_a, len(segs_adaptive))

    # Save benchmark result
    result = {
        "adaptive_duration_seconds": adaptive_dur,
        "adaptive_metrics": metrics_adaptive,
        "adaptive_main_y_pct": y_a,
        "adaptive_sub_segments": sub_segs_a,
        "total_segments": len(seg_dicts),
    }
    with open(out_dir / "ocr_benchmark_result.json", "w", encoding="utf-8") as f:
        json.dump(result, f, indent=2, ensure_ascii=False)
    logger.info("Saved benchmark result to %s", out_dir / "ocr_benchmark_result.json")
    return result


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--max-segments", type=int, default=None, help="Limit number of segments for quick probe")
    args = parser.parse_args()
    run_benchmark(args.max_segments)
