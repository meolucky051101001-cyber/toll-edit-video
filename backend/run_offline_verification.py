"""Run complete offline end-to-end verification job on long video benchmark.

This script executes the entire Tool V2 pipeline into an isolated benchmark directory
without touching Tool V1, without delivering to user production folders, and without
sending Telegram notifications.
"""

from __future__ import annotations

import argparse
import asyncio
import datetime
import json
import logging
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

# UTF-8 encoding for Windows console with write_through for instant flush
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True, write_through=True)
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace", line_buffering=True, write_through=True)

BASE_DIR = Path(__file__).resolve().parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

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
parser.add_argument(
    "--source-video",
    type=str,
    default=r"D:\video phôi\test_douyin_7686043439540030762.mp4",
    help="Path to input video file for benchmark",
)
parser.add_argument(
    "--cold",
    action="store_true",
    default=False,
    help="Enforce fresh 100%% cold benchmark in e2e_cold_verify_job",
)
parser.add_argument(
    "--warm",
    action="store_true",
    default=False,
    help="Run warm benchmark in e2e_verify_job",
)
parser.add_argument(
    "--rerender",
    action="store_true",
    default=False,
    help="In warm mode, invalidate from render stage to measure render+qc execution time",
)
parser.add_argument(
    "--benchmark-dir",
    type=str,
    default=None,
    help="Custom benchmark directory",
)
parser.add_argument(
    "--output-dir",
    type=str,
    default=None,
    help="Custom output directory",
)
parser.add_argument(
    "--clean-job",
    action="store_true",
    default=False,
    help="Explicitly wipe job directory before running",
)
parser.add_argument(
    "--resume",
    action="store_true",
    default=False,
    help="Resume pipeline from existing completed stage checkpoints",
)
parser.add_argument(
    "--invalidate-from",
    type=str,
    default=None,
    help="Invalidate stages from the specified stage onwards in the manifest",
)
cli_args, _ = parser.parse_known_args()

# Determine benchmark directories
is_warm_mode = bool(cli_args.warm)
benchmark_root = (
    Path(r"D:\workspace_v2\benchmark_runs")
    if Path(r"D:\workspace_v2").is_dir()
    else (BASE_DIR.parent / "workspace" / "benchmark_runs")
)
if cli_args.benchmark_dir:
    benchmark_dir = Path(cli_args.benchmark_dir).resolve()
elif is_warm_mode:
    benchmark_dir = (benchmark_root / "e2e_v4_warm_verify").resolve()
else:
    benchmark_dir = (benchmark_root / "e2e_v4_cold_verify").resolve()

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

from pipeline_v2.adaptive import probe_video_duration
from pipeline_v2.config import PipelineSettings
from pipeline_v2.video_pipeline import (
    PIPELINE_IMPLEMENTATION_VERSION,
    VideoPipelineRequest,
    VideoPipelineRunner,
    discover_rvc_model,
)


def get_git_status() -> dict:
    """Retrieve actual git commit hash and dirty status."""
    try:
        head = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=str(BASE_DIR.parent),
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        status = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=str(BASE_DIR.parent),
            capture_output=True,
            text=True,
        ).stdout.strip()
        diff = subprocess.run(
            ["git", "diff", "HEAD"],
            cwd=str(BASE_DIR.parent),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        ).stdout
        import hashlib
        diff_hash = hashlib.sha256(diff.encode("utf-8", errors="replace")).hexdigest()[:12] if diff else "clean"
        uncommitted = [line.strip() for line in status.splitlines() if line.strip()]
        return {
            "commit": head,
            "is_dirty": bool(uncommitted),
            "diff_hash": diff_hash,
            "uncommitted_count": len(uncommitted),
            "uncommitted_files": uncommitted,
        }
    except Exception as exc:
        return {"commit": "unknown", "error": str(exc)}


def extract_targeted_qc_frames(
    video_path: Path,
    output_dir: Path,
    timestamps: list[float],
) -> list[str]:
    """Extract frames at representative timestamps to verify visual layout and subtitle quality."""
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


def probe_media_info(file_path: Path) -> dict:
    """Probe media properties using ffprobe and calculate sha256."""
    if not file_path or not Path(file_path).is_file():
        return {}
    p = Path(file_path)
    import hashlib
    sha256 = ""
    try:
        h = hashlib.sha256()
        with open(p, "rb") as f:
            for chunk in iter(lambda: f.read(1024 * 1024), b""):
                h.update(chunk)
        sha256 = h.hexdigest()
    except Exception:
        pass

    probe_cmd = [
        "ffprobe",
        "-v", "error",
        "-show_entries", "stream=codec_type,codec_name,width,height,r_frame_rate,sample_rate,channels:format=duration,size,bit_rate",
        "-of", "json",
        str(p)
    ]
    try:
        res = subprocess.run(probe_cmd, capture_output=True, text=True, check=True)
        data = json.loads(res.stdout)
        streams = data.get("streams", [])
        fmt = data.get("format", {})
        video_stream = next((s for s in streams if s.get("codec_type") == "video"), {})
        audio_stream = next((s for s in streams if s.get("codec_type") == "audio"), {})

        r_fps = video_stream.get("r_frame_rate", "30/1")
        try:
            num, den = map(int, r_fps.split("/"))
            fps = round(num / den, 2) if den else 30.0
        except Exception:
            fps = 30.0

        return {
            "path": str(p),
            "size_bytes": p.stat().st_size,
            "sha256": sha256,
            "duration": round(float(fmt.get("duration", 0.0)), 2),
            "width": int(video_stream.get("width", 0)),
            "height": int(video_stream.get("height", 0)),
            "fps": fps,
            "video_codec": video_stream.get("codec_name", ""),
            "audio_codec": audio_stream.get("codec_name", ""),
            "audio_sample_rate": int(audio_stream.get("sample_rate", 0)),
            "audio_channels": int(audio_stream.get("channels", 0)),
        }
    except Exception as exc:
        return {"error": str(exc), "sha256": sha256, "size_bytes": p.stat().st_size}


def extract_first_dialogues(job_dir: Path, n: int = 5) -> list[dict]:
    """Extract first n dialogue segments with source Chinese and translated Vietnamese."""
    candidates = [
        job_dir / "pipeline_v2" / "artifacts" / "timing" / "timed_segments.json",
        job_dir / "pipeline_v2" / "artifacts" / "translation" / "segments.json",
        job_dir / "pipeline_v2" / "artifacts" / "transcript" / "segments.json",
        job_dir / "timed_segments.json",
    ]
    for c in candidates:
        if c.is_file():
            try:
                data = json.loads(c.read_text(encoding="utf-8"))
                segs = data.get("segments", data) if isinstance(data, dict) else data
                if isinstance(segs, list) and segs:
                    extracted = []
                    for seg in segs[:n]:
                        extracted.append({
                            "index": seg.get("index") or seg.get("id"),
                            "start": seg.get("start"),
                            "end": seg.get("end"),
                            "source_chinese": seg.get("orig_content") or seg.get("source") or seg.get("text"),
                            "translated_vietnamese": seg.get("content") or seg.get("text"),
                        })
                    return extracted
            except Exception:
                pass
    return []



async def main():
    source_video = Path(cli_args.source_video).resolve()
    if not source_video.is_file():
        logger.error(f"Source video not found: {source_video}")
        sys.exit(1)

    job_dir = benchmark_dir / "job"
    if cli_args.output_dir:
        output_dir = Path(cli_args.output_dir).resolve()
    else:
        output_dir = benchmark_dir / "output"
    output_dir.mkdir(parents=True, exist_ok=True)
    final_output_path = output_dir / f"verified_{source_video.stem}.mp4"

    # Enforce cold run if clean_job or cold requested (unless resuming)
    if not cli_args.resume and (cli_args.clean_job or (cli_args.cold and not is_warm_mode)):
        if job_dir.exists():
            logger.info(f"Cleaning existing job directory for fresh cold run: {job_dir}")
            shutil.rmtree(str(job_dir), ignore_errors=True)

    pipeline_v2_dir = job_dir / "pipeline_v2"
    manifest_file = pipeline_v2_dir / "job_manifest.json"

    if is_warm_mode and not job_dir.exists():
        seed_candidates = [
            benchmark_root / "e2e_v4_warm_verify" / "job",
            benchmark_root / "e2e_v4_cold_verify" / "job",
            benchmark_root / "e2e_v3_cold_verify" / "job",
            benchmark_root / "e2e_true_cold_verify" / "job",
        ]
        for candidate in seed_candidates:
            if candidate.exists():
                logger.info(f"Seeding warm job directory from run: {candidate} -> {job_dir}")
                shutil.copytree(str(candidate), str(job_dir))
                break

    if is_warm_mode and manifest_file.is_file():
        from pipeline_v2.manifest import ManifestStore
        mstore = ManifestStore(pipeline_v2_dir)
        m = mstore.load()
        from pipeline_v2.video_pipeline import V2_STAGE_ORDER
        if cli_args.rerender:
            m.invalidate_from("render", V2_STAGE_ORDER)
            logger.info("Invalidated stages from 'render' for warm render+qc measurement")
        else:
            m.invalidate_from("subtitles", V2_STAGE_ORDER)
            logger.info("Invalidated stages from 'subtitles' for warm subtitle+render+qc measurement (mix_v2 audio cache hit)")
        mstore.save(m)
    elif cli_args.invalidate_from and manifest_file.is_file():
        from pipeline_v2.manifest import ManifestStore
        mstore = ManifestStore(pipeline_v2_dir)
        m = mstore.load()
        from pipeline_v2.video_pipeline import V2_STAGE_ORDER
        m.invalidate_from(cli_args.invalidate_from, V2_STAGE_ORDER)
        logger.info(f"Invalidated stages from '{cli_args.invalidate_from}'")
        mstore.save(m)
        # Also clean downstream artifacts to ensure fresh generation
        artifacts_dir = pipeline_v2_dir / "artifacts"
        if cli_args.invalidate_from in ("timing", "translate", "ocr"):
            gap_rec = artifacts_dir / "ocr" / "gap_reconciliation.json"
            if gap_rec.is_file():
                gap_rec.unlink(missing_ok=True)
            timed_segs = artifacts_dir / "translation" / "timed_segments.json"
            if timed_segs.is_file():
                timed_segs.unlink(missing_ok=True)
        for sub_dir_name in ("tts", "rvc", "subtitles", "mix_v2", "output", "qc"):
            sd = artifacts_dir / sub_dir_name
            if sd.is_dir():
                shutil.rmtree(str(sd), ignore_errors=True)
        if final_output_path.is_file():
            final_output_path.unlink(missing_ok=True)

    # Discover RVC Model if available
    rvc_model = discover_rvc_model(Path(r"C:\tool v2\MyVoiceModel_v2"))
    if not rvc_model:
        rvc_model = discover_rvc_model(Path(r"C:\tool v2\workspace"))

    env_override = dict(os.environ)
    if is_warm_mode or cli_args.resume:
        env_override["ENABLE_STAGE_CACHE"] = "1"
    settings = PipelineSettings.from_env(env_override)

    # Video duration
    source_duration = probe_video_duration(source_video) or 0.0
    git_info = get_git_status()
    git_commit = git_info.get("commit", "unknown")

    logger.info("=" * 80)
    mode_str = "WARM BENCHMARK (CACHE REUSE)" if is_warm_mode else "100% COLD BENCHMARK (FRESH)"
    logger.info(f"OFFLINE END-TO-END VERIFICATION RUN — PIPELINE V{PIPELINE_IMPLEMENTATION_VERSION} [{mode_str}]")
    logger.info("=" * 80)
    logger.info(f"Git Commit: {git_commit} (dirty={git_info.get('is_dirty')}, uncommitted={git_info.get('uncommitted_count')})")
    logger.info(f"Source Video: {source_video}")
    logger.info(f"Source Size: {source_video.stat().st_size / (1024*1024):.2f} MB")
    logger.info(f"Source Duration: {source_duration:.2f}s ({source_duration / 60.0:.2f} minutes)")
    logger.info(f"Benchmark Dir: {benchmark_dir}")
    logger.info(f"RVC Model: {rvc_model}")
    logger.info(f"Settings: QC_GATE_POLICY={settings.qc_gate_policy}, "
                f"RVC_BATCH_SEGMENTS={settings.rvc_batch_segments}, "
                f"TTS_BATCH_SEGMENTS={settings.tts_batch_segments}, "
                f"ENABLE_ADAPTIVE_OCR={settings.enable_adaptive_ocr}")
    logger.info("=" * 80)

    start_wall_time = time.time()
    stage_start_times: Dict[str, float] = {}
    real_stage_durations: Dict[str, float] = {}

    def on_progress(stage: str, state: str):
        elapsed = int(time.time() - start_wall_time)
        m, s = divmod(elapsed, 60)
        logger.info(f"⏱️ [{m:02d}:{s:02d}] Stage: {stage} -> {state}")
        now = time.time()
        if state == "running":
            stage_start_times[stage] = now
        elif state in ("completed", "cache_hit", "failed", "skipped"):
            st = stage_start_times.pop(stage, None)
            if st is not None:
                real_stage_durations[stage] = round(now - st, 2)
            elif state == "cache_hit":
                real_stage_durations[stage] = 0.0

    async def async_progress(stage: str, state: str):
        on_progress(stage, state)

    request = VideoPipelineRequest(
        video_path=source_video,
        job_directory=job_dir,
        output_path=final_output_path,
        delivery_copy_path=None,
        settings=settings,
        api_key=os.getenv("GEMINI_API_KEY", ""),
        voice_source="rvc" if rvc_model else "edge",
        voice_param=str(rvc_model) if rvc_model else "vi-VN-HoaiMyNeural",
        rvc_model_path=rvc_model,
        progress=async_progress,
    )

    from pipeline_v2.video_pipeline import QCGateBlocked

    try:
        runner = VideoPipelineRunner(request)
        result = await runner.run()
        qc_allowed = result.qc_allowed
        qc_reason = result.qc_reason
        qc_report_path = Path(result.qc_report_path)
    except QCGateBlocked as exc:
        logger.warning(f"Pipeline QC Gate blocked delivery as strictly required by BLOCK policy: {exc}")
        qc_allowed = False
        qc_reason = str(exc)
        qc_report_path = pipeline_v2_dir / "artifacts" / "qc" / "qc_report.json"
    except Exception as exc:
        logger.error(f"Pipeline run raised exception: {exc}", exc_info=True)
        raise

    total_wall_time = time.time() - start_wall_time
    total_m, total_s = divmod(int(total_wall_time), 60)
    rtf = total_wall_time / max(0.1, source_duration)

    actual_video_path = final_output_path
    if not actual_video_path.is_file():
        internal_output = pipeline_v2_dir / "artifacts" / "output" / "final.mp4"
        if internal_output.is_file():
            actual_video_path = internal_output

    actual_size = actual_video_path.stat().st_size if actual_video_path.is_file() else 0

    logger.info("=" * 80)
    logger.info(f"PIPELINE RUN COMPLETED IN {total_m}m {total_s}s (Total: {total_wall_time:.1f}s, RTF: {rtf:.3f}x)")
    logger.info(f"Rendered Output Video: {actual_video_path} (Size: {actual_size / (1024*1024):.2f} MB)")
    logger.info(f"QC Allowed: {qc_allowed}")
    logger.info(f"QC Reason: {qc_reason}")
    logger.info(f"QC Report: {qc_report_path}")
    logger.info("=" * 80)

    # Parse stage timings from manifest
    manifest_stages = {}
    if manifest_file.is_file():
        try:
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
        except Exception as e:
            logger.warning(f"Could not parse manifest stages: {e}")

    # Read QC Report
    qc_metrics = {}
    if qc_report_path and Path(qc_report_path).is_file():
        try:
            qc_metrics = json.loads(Path(qc_report_path).read_text(encoding="utf-8"))
        except Exception:
            pass

    # Targeted timestamps for regression anchors and gap dialogue recovery
    sample_timestamps = [0.50, 5.00, 15.00, 25.00, 117.40, 144.32, 150.32, 321.17, 393.60, 636.98, 978.23, 979.50, 980.50, 1087.66]
    if source_duration > 0:
        pcts = [0.10, 0.25, 0.50, 0.70, 0.85, 0.95]
        for p in pcts:
            sample_timestamps.append(round(p * source_duration, 2))
    sample_timestamps = sorted(set(sample_timestamps))

    extracted_frames = extract_targeted_qc_frames(
        actual_video_path,
        benchmark_dir / "visual_verification",
        sample_timestamps,
    )

    # Read timed segments count
    segment_count = 0
    candidate_seg_files = [
        pipeline_v2_dir / "artifacts" / "translation" / "timed_segments.json",
        pipeline_v2_dir / "artifacts" / "timed_segments.json",
        pipeline_v2_dir / "timed_segments.json",
        job_dir / "timed_segments.json",
        job_dir / "timing" / "result.json",
        pipeline_v2_dir / "artifacts" / "qc" / "segments.json",
    ]
    for csf in candidate_seg_files:
        if csf.is_file():
            try:
                segs = json.loads(csf.read_text(encoding="utf-8"))
                if isinstance(segs, list):
                    segment_count = len(segs)
                elif isinstance(segs, dict) and "segments" in segs:
                    segment_count = len(segs["segments"])
                if segment_count > 0:
                    break
            except Exception:
                pass

    source_media_info = probe_media_info(source_video)
    final_media_info = probe_media_info(actual_video_path)
    first_five_dialogues = extract_first_dialogues(job_dir, n=5)

    summary = {
        "status": "success" if qc_allowed else "qc_blocked",
        "git_status": git_info,
        "pipeline_version": PIPELINE_IMPLEMENTATION_VERSION,
        "benchmark_mode": "warm" if is_warm_mode else "cold",
        "source_video": str(source_video),
        "source_duration_seconds": round(source_duration, 2),
        "source_media_info": source_media_info,
        "final_media_info": final_media_info,
        "segment_count": segment_count,
        "first_five_dialogues": first_five_dialogues,
        "total_wall_time_seconds": round(total_wall_time, 2),
        "total_wall_time_formatted": f"{total_m}m {total_s}s",
        "real_time_factor_rtf": round(rtf, 4),
        "real_stage_durations_seconds": real_stage_durations,
        "manifest_stage_durations_seconds": manifest_stages,
        "qc_allowed": qc_allowed,
        "qc_reason": qc_reason,
        "qc_report_path": str(qc_report_path),
        "extracted_qc_frames": extracted_frames,
        "final_video_path": str(actual_video_path),
        "final_video_size_bytes": actual_size,
    }

    summary_file = benchmark_dir / "verification_summary.json"
    summary_file.write_text(json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8")
    logger.info(f"Verification summary saved to: {summary_file}")
    print(json.dumps(summary, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    asyncio.run(main())
