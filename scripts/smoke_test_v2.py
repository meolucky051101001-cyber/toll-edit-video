"""End-to-end smoke test for Tool V2 production pipeline."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import shutil
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

from environment import read_environment
from pipeline_v2.config import PipelineSettings
from pipeline_v2.video_pipeline import VideoPipelineRequest, VideoPipelineRunner


def enforce_clean_smoke_directory(work_dir: Path, retries: int = 5, delay: float = 0.5) -> None:
    """Fail-closed cleanup of the smoke test workspace to ensure clean cold execution."""
    if not work_dir.exists():
        work_dir.mkdir(parents=True, exist_ok=True)
        return

    print(f"Cleaning previous smoke test directory: {work_dir} (enforcing fail-closed cold run)...")
    last_err = None
    for attempt in range(1, retries + 1):
        try:
            shutil.rmtree(work_dir)
            break
        except Exception as err:
            last_err = err
            time.sleep(delay)

    if work_dir.exists():
        remaining = list(work_dir.iterdir())
        if remaining:
            raise RuntimeError(
                f"Fail-closed: Smoke test directory '{work_dir}' could not be cleaned. "
                f"Remaining entries: {[p.name for p in remaining]}. Error: {last_err}"
            )
        try:
            work_dir.rmdir()
        except Exception:
            pass

    work_dir.mkdir(parents=True, exist_ok=True)
    print(f"Verified smoke test workspace is clean: {work_dir}")


def verify_stages_freshness(
    manifest, run_start_utc: datetime, tolerance_seconds: float = 2.0
) -> list[str]:
    """Verify that all completed stages in the manifest were executed in this run."""
    min_allowed_time = run_start_utc - timedelta(seconds=tolerance_seconds)
    run_start_iso = run_start_utc.isoformat()
    stale_stages: list[str] = []
    for s_name, stage in manifest.stages.items():
        if stage.status.value == "completed":
            if not stage.started_at:
                stale_stages.append(f"{s_name} (missing started_at)")
                continue
            try:
                stage_time = datetime.fromisoformat(stage.started_at.replace("Z", "+00:00"))
                if stage_time < min_allowed_time:
                    stale_stages.append(
                        f"{s_name} (started_at {stage.started_at} < run_start {run_start_iso})"
                    )
            except Exception as parse_err:
                stale_stages.append(f"{s_name} (timestamp parse error: {parse_err})")
    return stale_stages


async def run_smoke_test():
    env = read_environment(ROOT / "backend")
    settings = PipelineSettings.from_env(env)
    print(f"Loaded pipeline settings: mode={settings.mode}, auto_gender={settings.enable_auto_gender}")

    downloads = ROOT / "workspace" / "downloads"
    sample_videos = list(downloads.glob("queue_9b27caac*.mp4"))
    if not sample_videos:
        sample_videos = list(downloads.glob("*.mp4"))
    if not sample_videos:
        print("Error: No sample video found in workspace/downloads.")
        return 1

    source_video = sample_videos[0]
    print(f"Using source video: {source_video.name}")

    work_dir = ROOT / "workspace" / "smoke_test_run"
    enforce_clean_smoke_directory(work_dir)
    out_video = work_dir / "Dubbed_smoke_test.mp4"

    request = VideoPipelineRequest(
        video_path=source_video,
        job_directory=work_dir / "job",
        output_path=out_video,
        settings=settings,
        api_key=env.get("GEMINI_API_KEY", ""),
        voice_source="edge",
        voice_param="vi-VN-HoaiMyNeural",
    )

    run_start_utc = datetime.now(timezone.utc)
    run_start_iso = run_start_utc.isoformat()
    print(f"Recorded smoke test start time (UTC): {run_start_iso}")

    runner = VideoPipelineRunner(request)
    print("Starting VideoPipelineRunner...")
    start_time = time.time()
    result = await runner.run()
    elapsed = time.time() - start_time
    print(f"Pipeline completed in {elapsed:.1f}s. Result: {result}")

    # Verify output video
    if not out_video.is_file() or out_video.stat().st_size == 0:
        print(f"FAIL: Output video not generated or empty at {out_video}")
        return 1
    size_mb = out_video.stat().st_size / (1024 * 1024)
    print(f"SUCCESS: Output video size = {size_mb:.2f} MB")

    # Verify transcript artifacts
    segments_file = runner.artifact_store.path_for("transcript/segments.json")
    if not segments_file.is_file():
        print(f"FAIL: segments.json missing at {segments_file}")
        return 1
    seg_data = json.loads(segments_file.read_text(encoding="utf-8"))
    segs = seg_data.get("segments", [])
    if not segs:
        print("FAIL: segments.json contains 0 segments")
        return 1
    print(f"SUCCESS: Transcript segments = {len(segs)}")
    print(f"  Sample segment 0: gender={segs[0].get('gender')}, speaker_id={segs[0].get('speaker_id')}")

    # Verify QC report
    qc_file = runner.artifact_store.path_for("qc/qc_report.json")
    if not qc_file.is_file():
        print(f"FAIL: QC report artifact missing at {qc_file}")
        return 1

    qc_data = json.loads(qc_file.read_text(encoding="utf-8"))
    print(f"SUCCESS: QC summary = {qc_data.get('summary')}")

    # Verify QC gate allowed
    if not getattr(result, "qc_allowed", False):
        print(f"FAIL: QC gate blocked delivery! reason={getattr(result, 'reason', 'unknown')}")
        return 1
    print(f"SUCCESS: QC gate allowed delivery = {result.qc_allowed}")

    # Verify all 13 checks
    checks = qc_data.get("checks", [])
    print(f"SUCCESS: Total QC checks evaluated = {len(checks)}")
    if len(checks) < 13:
        print(f"FAIL: Expected at least 13 QC checks, got {len(checks)}")
        return 1

    error_checks = [c for c in checks if c.get("status") == "error"]
    if error_checks:
        print(f"FAIL: Blocking QC errors found: {[c.get('name') for c in error_checks]}")
        return 1

    pixel_check = next((c for c in checks if c.get("name") == "pixel_cover_qc"), None)
    if not pixel_check:
        print("FAIL: pixel_cover_qc check is missing from QC report")
        return 1
    if pixel_check.get("status") != "pass":
        print(f"FAIL: pixel_cover_qc check status is {pixel_check.get('status')}, expected 'pass'")
        return 1
    print(f"SUCCESS: pixel_cover_qc check passed: {pixel_check.get('message')}")

    if "pixel_cover_qc" in qc_data.get("metrics", {}):
        print(f"SUCCESS: Pixel cover QC metric = {qc_data['metrics']['pixel_cover_qc']}")

    tts_check = next((c for c in checks if c.get("name") == "tts_integrity"), None)
    if not tts_check:
        print("FAIL: tts_integrity check is missing from QC report")
        return 1
    if tts_check.get("status") != "pass":
        print(f"FAIL: tts_integrity check status is {tts_check.get('status')}, expected 'pass'")
        return 1
    print(f"SUCCESS: tts_integrity check passed: {tts_check.get('message')}")

    # Verify manifest and pipeline version
    manifest = runner.manifest
    if not manifest:
        print("FAIL: Runner manifest is missing!")
        return 1
    pipe_ver = manifest.metadata.get("pipeline_implementation_version")
    print(f"SUCCESS: Pipeline implementation version = {pipe_ver}")
    if pipe_ver != "2.6.0":
        print(f"FAIL: Expected pipeline version 2.6.0, got {pipe_ver}")
        return 1

    manifest_created_at = manifest.created_at
    print(f"SUCCESS: Manifest created_at = {manifest_created_at}")

    # Programmatic assertion: every completed stage must be freshly executed in this run
    stage_summaries = {}
    for s_name, stage in manifest.stages.items():
        stage_summaries[s_name] = {
            "status": stage.status.value,
            "started_at": stage.started_at,
            "finished_at": stage.finished_at,
        }
        if stage.status.value == "completed":
            print(f"  Stage '{s_name}': {stage.started_at} -> {stage.finished_at}")

    stale_stages = verify_stages_freshness(manifest, run_start_utc)
    if stale_stages:
        print(f"FAIL: Cold smoke test assertion failed! Stale cached stages detected: {stale_stages}")
        return 1
    completed_count = len([s for s in manifest.stages.values() if s.status.value == "completed"])
    print(
        f"SUCCESS: All {completed_count} completed stages programmatically verified fresh "
        f"(started_at >= run_start_utc: {run_start_iso})"
    )

    # Save summary manifest for audit inspection
    summary = {
        "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
        "run_start_utc": run_start_iso,
        "cold_verified": True,
        "status": "PASS",
        "output_video": str(out_video),
        "video_size_mb": round(size_mb, 2),
        "elapsed_seconds": round(elapsed, 2),
        "segments_count": len(segs),
        "qc_allowed": result.qc_allowed,
        "qc_checks_count": len(checks),
        "checks": [c.get("name") for c in checks],
        "pipeline_implementation_version": pipe_ver,
        "manifest_created_at": manifest_created_at,
        "stages": stage_summaries,
    }
    (work_dir / "smoke_summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    print(f"SUCCESS: Smoke test summary saved to: {work_dir / 'smoke_summary.json'}")
    return 0


if __name__ == "__main__":
    code = asyncio.run(run_smoke_test())
    sys.exit(code)
