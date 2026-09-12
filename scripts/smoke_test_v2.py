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
    if not out_video.is_file():
        print(f"FAIL: Output video not generated at {out_video}")
        return 1
    size_mb = out_video.stat().st_size / (1024 * 1024)
    print(f"SUCCESS: Output video size = {size_mb:.2f} MB")

    # Verify transcript artifacts
    job_dir = work_dir / "job"
    segments_file = runner.artifact_store.path_for("transcript/segments.json")
    if segments_file.is_file():
        seg_data = json.loads(segments_file.read_text(encoding="utf-8"))
        segs = seg_data.get("segments", [])
        print(f"SUCCESS: Transcript segments = {len(segs)}")
        if segs:
            print(f"  Sample segment 0: gender={segs[0].get('gender')}, speaker_id={segs[0].get('speaker_id')}")

    # Verify QC report
    qc_file = runner.artifact_store.path_for("artifacts/qc/qc_report.json")
    if not qc_file.is_file():
        qc_file = job_dir / "qc" / "qc_report.json"
    if qc_file.is_file():
        qc_data = json.loads(qc_file.read_text(encoding="utf-8"))
        print(f"SUCCESS: QC summary = {qc_data.get('summary')}")
        if "pixel_cover_qc" in qc_data.get("metrics", {}):
            print(f"SUCCESS: Pixel cover QC metric = {qc_data['metrics']['pixel_cover_qc']}")

    return 0


if __name__ == "__main__":
    code = asyncio.run(run_smoke_test())
    sys.exit(code)
