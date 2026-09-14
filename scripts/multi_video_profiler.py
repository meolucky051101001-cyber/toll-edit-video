"""Multi-video performance profiler and bottleneck analyzer for Tool V2."""

from __future__ import annotations

import argparse
import asyncio
from dataclasses import replace
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import shutil
import sys
import time
from typing import Any, Dict, List, Optional

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

from environment import read_environment
from pipeline_v2.config import PipelineSettings
from pipeline_v2.video_pipeline import VideoPipelineRequest, VideoPipelineRunner


def probe_video_info(video_path: Path) -> Dict[str, Any]:
    import subprocess
    cmd = [
        "ffprobe",
        "-v", "quiet",
        "-print_format", "json",
        "-show_format",
        "-show_streams",
        str(video_path),
    ]
    p = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if p.returncode == 0:
        data = json.loads(p.stdout)
        dur = float(data.get("format", {}).get("duration", 0.0))
        video_stream = next((s for s in data.get("streams", []) if s.get("codec_type") == "video"), {})
        w = int(video_stream.get("width", 0))
        h = int(video_stream.get("height", 0))
        return {"duration": round(dur, 2), "width": w, "height": h, "resolution": f"{w}x{h}"}
    return {"duration": 0.0, "width": 0, "height": 0, "resolution": "unknown"}


async def profile_single_video(
    video_path: Path,
    label: str,
    with_rvc: bool = False,
    output_base: Optional[Path] = None,
) -> Dict[str, Any]:
    env = read_environment(ROOT / "backend")
    settings = PipelineSettings.from_env(env)
    if with_rvc:
        settings = replace(settings, enable_rvc=True)

    info = probe_video_info(video_path)
    print(f"\n======================================================================")
    print(f"PROFILING: {label} ({video_path.name})")
    print(f"Resolution: {info['resolution']}, Duration: {info['duration']}s, RVC: {with_rvc}")
    print(f"======================================================================")

    out_base = output_base or (ROOT / "workspace" / f"profiler_{label}_{int(time.time())}")
    if out_base.exists():
        shutil.rmtree(out_base, ignore_errors=True)
    out_base.mkdir(parents=True, exist_ok=True)

    out_video = out_base / f"output_{label}.mp4"
    rvc_model = (ROOT / "MyVoiceModel_v2" / "mi-giong_cua_toi_v2.pth") if with_rvc else None
    voice_source = "rvc" if with_rvc else "edge"
    voice_param = str(rvc_model) if with_rvc else "vi-VN-HoaiMyNeural"

    request = VideoPipelineRequest(
        video_path=video_path,
        job_directory=out_base / "job",
        output_path=out_video,
        settings=settings,
        api_key=env.get("GEMINI_API_KEY", ""),
        voice_source=voice_source,
        voice_param=voice_param,
        rvc_model_path=rvc_model,
    )

    t0 = time.time()
    runner = VideoPipelineRunner(request)
    qc_allowed = False
    qc_blocked_reason = None
    try:
        result = await runner.run()
        qc_allowed = getattr(result, "qc_allowed", False)
    except Exception as exc:
        qc_allowed = False
        qc_blocked_reason = str(exc)
        print(f"QC GATE INTERCEPTED: Blocked delivery - {exc}")
    elapsed = time.time() - t0

    manifest = runner.manifest
    stage_timings: Dict[str, float] = {}
    if manifest:
        for s_name, stage in manifest.stages.items():
            if stage.status.value == "completed" and stage.started_at and stage.finished_at:
                try:
                    ts_start = datetime.fromisoformat(stage.started_at.replace("Z", "+00:00"))
                    ts_end = datetime.fromisoformat(stage.finished_at.replace("Z", "+00:00"))
                    dur = round((ts_end - ts_start).total_seconds(), 2)
                    stage_timings[s_name] = dur
                except Exception:
                    stage_timings[s_name] = 0.0
            else:
                stage_timings[s_name] = 0.0

    # Read QC metrics and boundary frames
    qc_metrics: Dict[str, Any] = {}
    boundary_frames_checked = 0
    pixel_cover_pass = False
    true_peak_val = None
    qc_file = runner.artifact_store.path_for("qc/qc_report.json")
    if qc_file.is_file():
        try:
            qc_data = json.loads(qc_file.read_text(encoding="utf-8"))
            qc_metrics = qc_data.get("metrics", {})
            pix_metric = qc_metrics.get("pixel_cover_qc", {})
            boundary_frames_checked = pix_metric.get("checked_frames", 0)
            pixel_cover_pass = pix_metric.get("all_boxes_filled", False)
            tp_check = next((c for c in qc_data.get("checks", []) if c.get("name") == "audio_true_peak"), None)
            if tp_check and "metrics" in tp_check:
                true_peak_val = tp_check["metrics"].get("true_peak_dbtp")
        except Exception:
            pass

    whisper_time = stage_timings.get("transcribe", 0.0)
    demucs_time = stage_timings.get("demucs", 0.0)
    ai_heavy_time = whisper_time + demucs_time
    total_pipeline_time = round(elapsed, 2)
    ai_percentage = round((ai_heavy_time / total_pipeline_time * 100.0), 1) if total_pipeline_time > 0 else 0.0

    print(f"Result for {label}:")
    print(f"  Total Elapsed: {total_pipeline_time}s")
    if total_pipeline_time > 0:
        print(f"  Whisper large-v3: {whisper_time}s ({whisper_time / total_pipeline_time * 100:.1f}%)")
        print(f"  Demucs htdemucs: {demucs_time}s ({demucs_time / total_pipeline_time * 100:.1f}%)")
    print(f"  Combined (Whisper + Demucs): {ai_heavy_time}s ({ai_percentage}%)")
    if with_rvc:
        rvc_time = stage_timings.get("rvc", 0.0)
        print(f"  RVC Voice Conversion: {rvc_time}s ({rvc_time / total_pipeline_time * 100:.1f}%)")
    print(f"  Boundary Frames Inspected: {boundary_frames_checked} (Pixel cover pass: {pixel_cover_pass})")
    print(f"  True Peak: {true_peak_val} dBTP")
    print(f"  QC Allowed: {qc_allowed}")
    if qc_blocked_reason:
        print(f"  QC Blocked Reason: {qc_blocked_reason}")

    return {
        "label": label,
        "video_file": video_path.name,
        "resolution": info["resolution"],
        "video_duration_seconds": info["duration"],
        "with_rvc": with_rvc,
        "total_elapsed_seconds": total_pipeline_time,
        "realtime_ratio": round(total_pipeline_time / max(0.1, info["duration"]), 2),
        "stage_timings": stage_timings,
        "bottlenecks": {
            "whisper_seconds": whisper_time,
            "demucs_seconds": demucs_time,
            "combined_seconds": ai_heavy_time,
            "ai_heavy_percentage": ai_percentage,
        },
        "qc": {
            "boundary_frames_checked": boundary_frames_checked,
            "pixel_cover_pass": pixel_cover_pass,
            "true_peak_dbtp": true_peak_val,
            "qc_allowed": qc_allowed,
            "qc_blocked_reason": qc_blocked_reason,
        },
    }


def collect_from_workspace_dir(
    dir_path: Path,
    video_path: Path,
    label: str,
    with_rvc: bool = False,
) -> Dict[str, Any]:
    info = probe_video_info(video_path)
    manifest_f = dir_path / "job" / "pipeline_v2" / "job_manifest.json"
    qc_f = dir_path / "job" / "pipeline_v2" / "artifacts" / "qc" / "qc_report.json"

    stage_timings: Dict[str, float] = {}
    total_pipeline_time = 0.0
    if manifest_f.exists():
        data = json.loads(manifest_f.read_text(encoding="utf-8"))
        stages = data.get("stages", {})
        all_starts = []
        all_ends = []
        for s_name, stage in stages.items():
            s_at = stage.get("started_at")
            f_at = stage.get("finished_at")
            st = stage.get("status")
            if (st == "completed" or stage.get("status_value") == "completed") and s_at and f_at:
                try:
                    ts_start = datetime.fromisoformat(s_at.replace("Z", "+00:00"))
                    ts_end = datetime.fromisoformat(f_at.replace("Z", "+00:00"))
                    dur = round((ts_end - ts_start).total_seconds(), 2)
                    stage_timings[s_name] = dur
                    all_starts.append(ts_start)
                    all_ends.append(ts_end)
                except Exception:
                    stage_timings[s_name] = 0.0
            else:
                stage_timings[s_name] = 0.0
        if all_starts and all_ends:
            total_pipeline_time = round((max(all_ends) - min(all_starts)).total_seconds(), 2)

    qc_metrics: Dict[str, Any] = {}
    boundary_frames_checked = 0
    pixel_cover_pass = False
    true_peak_val = None
    qc_allowed = False
    qc_blocked_reason = None

    if qc_f.is_file():
        try:
            qc_data = json.loads(qc_f.read_text(encoding="utf-8"))
            qc_allowed = qc_data.get("delivery_allowed", False)
            qc_metrics = qc_data.get("metrics", {})
            pix_metric = qc_metrics.get("pixel_cover_qc", {})
            boundary_frames_checked = pix_metric.get("checked_frames", 0)
            pixel_cover_pass = pix_metric.get("all_boxes_filled", False)
            tp_check = next((c for c in qc_data.get("checks", []) if c.get("name") == "audio_true_peak"), None)
            if tp_check and "metrics" in tp_check:
                true_peak_val = tp_check["metrics"].get("true_peak_dbtp")

            blocking_checks = [c["name"] for c in qc_data.get("checks", []) if c.get("status") == "error"]
            if blocking_checks:
                qc_allowed = False
                qc_blocked_reason = f"QC gate blocked delivery because critical errors were reported: {', '.join(blocking_checks)}"
        except Exception:
            pass

    whisper_time = stage_timings.get("transcribe", 0.0)
    demucs_time = stage_timings.get("demucs", 0.0)
    ai_heavy_time = whisper_time + demucs_time
    ai_percentage = round((ai_heavy_time / total_pipeline_time * 100.0), 1) if total_pipeline_time > 0 else 0.0

    print(f"Result for {label} (from {dir_path.name}):")
    print(f"  Total Elapsed: {total_pipeline_time}s")
    if total_pipeline_time > 0:
        print(f"  Whisper large-v3: {whisper_time}s ({whisper_time / total_pipeline_time * 100:.1f}%)")
        print(f"  Demucs htdemucs: {demucs_time}s ({demucs_time / total_pipeline_time * 100:.1f}%)")
    print(f"  Combined (Whisper + Demucs): {ai_heavy_time}s ({ai_percentage}%)")
    if with_rvc:
        rvc_time = stage_timings.get("rvc", 0.0)
        print(f"  RVC Voice Conversion: {rvc_time}s ({rvc_time / total_pipeline_time * 100:.1f}%)")
    print(f"  Boundary Frames Inspected: {boundary_frames_checked} (Pixel cover pass: {pixel_cover_pass})")
    print(f"  True Peak: {true_peak_val} dBTP")
    print(f"  QC Allowed: {qc_allowed}")
    if qc_blocked_reason:
        print(f"  QC Blocked Reason: {qc_blocked_reason}")

    return {
        "label": label,
        "video_file": video_path.name,
        "resolution": info["resolution"],
        "video_duration_seconds": info["duration"],
        "with_rvc": with_rvc,
        "total_elapsed_seconds": total_pipeline_time,
        "realtime_ratio": round(total_pipeline_time / max(0.1, info["duration"]), 2),
        "stage_timings": stage_timings,
        "bottlenecks": {
            "whisper_seconds": whisper_time,
            "demucs_seconds": demucs_time,
            "combined_seconds": ai_heavy_time,
            "ai_heavy_percentage": ai_percentage,
        },
        "qc": {
            "boundary_frames_checked": boundary_frames_checked,
            "pixel_cover_pass": pixel_cover_pass,
            "true_peak_dbtp": true_peak_val,
            "qc_allowed": qc_allowed,
            "qc_blocked_reason": qc_blocked_reason,
        },
    }


async def main():
    parser = argparse.ArgumentParser(description="Multi-video profiler for Tool V2")
    parser.add_argument("--out", type=str, default="benchmarks/multi_video_benchmark_report.json")
    parser.add_argument("--collect-existing", action="store_true", help="Collect metrics from recent profiler runs in workspace")
    args = parser.parse_args()

    downloads = ROOT / "workspace" / "downloads"
    video_4k_a = list(downloads.glob("queue_9b27caac*.mp4"))
    video_4k_b = list(downloads.glob("queue_28e175ed*.mp4"))
    video_1080p = list(downloads.glob("queue_d0ac9434*.mp4"))

    results = []

    if args.collect_existing or not sys.argv[1:]:
        existing_a = sorted(ROOT.glob("workspace/profiler_4K_Video_A_RVC_*"))
        existing_b = sorted(ROOT.glob("workspace/profiler_4K_Video_B_New_*"))
        existing_c = sorted(ROOT.glob("workspace/profiler_1080p_Video_New_*"))
        if existing_a and existing_b and existing_c:
            print("Collecting benchmark and profiling data from recent runs in workspace...")
            r1 = collect_from_workspace_dir(existing_a[-1], video_4k_a[0], "4K_Video_A_RVC", with_rvc=True)
            results.append(r1)
            r2 = collect_from_workspace_dir(existing_b[-1], video_4k_b[0], "4K_Video_B_New", with_rvc=False)
            results.append(r2)
            r3 = collect_from_workspace_dir(existing_c[-1], video_1080p[0], "1080p_Video_New", with_rvc=False)
            results.append(r3)
        else:
            args.collect_existing = False

    if not results:
        # 1. 4K Video A with RVC
        if video_4k_a and video_4k_a[0].is_file():
            r1 = await profile_single_video(video_4k_a[0], "4K_Video_A_RVC", with_rvc=True)
            results.append(r1)

        # 2. 4K Video B (Fresh new 4K video) with Edge TTS
        if video_4k_b and video_4k_b[0].is_file():
            r2 = await profile_single_video(video_4k_b[0], "4K_Video_B_New", with_rvc=False)
            results.append(r2)

        # 3. 1080p Video with Edge TTS
        if video_1080p and video_1080p[0].is_file():
            r3 = await profile_single_video(video_1080p[0], "1080p_Video_New", with_rvc=False)
            results.append(r3)

    report_path = ROOT / args.out
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_data = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "summary": "Multi-video production benchmark with Whisper/Demucs bottleneck analysis and RVC",
        "results": results,
    }
    report_path.write_text(json.dumps(report_data, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"\nSUCCESS: Multi-video profiling report saved to: {report_path}")


if __name__ == "__main__":
    asyncio.run(main())
