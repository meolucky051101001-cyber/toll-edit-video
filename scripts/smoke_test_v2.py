"""End-to-end smoke test for Tool V2 production pipeline."""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
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
    work_dir.mkdir(parents=True, exist_ok=True)
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

    # Save summary manifest for audit inspection
    summary = {
        "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
        "status": "PASS",
        "output_video": str(out_video),
        "video_size_mb": round(size_mb, 2),
        "elapsed_seconds": round(elapsed, 2),
        "segments_count": len(segs),
        "qc_allowed": result.qc_allowed,
        "qc_checks_count": len(checks),
        "checks": [c.get("name") for c in checks],
    }
    (work_dir / "smoke_summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    print(f"SUCCESS: Smoke test summary saved to: {work_dir / 'smoke_summary.json'}")
    return 0


if __name__ == "__main__":
    code = asyncio.run(run_smoke_test())
    sys.exit(code)
