import asyncio
import sys
import json
import shutil
from pathlib import Path

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

backend = Path(__file__).resolve().parent
sys.path.insert(0, str(backend))

from v1_orchestrator import V1Orchestrator
import job_tracker

async def main():
    workspace = Path(r"D:\workspace")
    job_id = "telegram_d6a7be49d32f4eafa3750b62d176c299"
    out_dir = workspace / "telegram_d6a7be49d32f4eafa3750b62d176c299_眼画瞎手画废画的小东真受罪"
    manifest_path = workspace / "bot_system" / "manifests" / f"{job_id}.manifest.json"

    # Remove artifacts from ASR onwards so they recompute cleanly
    files_to_clean = [
        "asr.json", "original.asr.json", "original.srt", "original.srt.v1-cache.json",
        "ocr.json", "ocr_geometry_report.json",
        "translated.srt", "translation.json",
        "tts.json", "mixed.wav", "final.ass"
    ]
    for fname in files_to_clean:
        p = out_dir / fname
        if p.exists():
            p.unlink()
            print(f"Removed: {fname}")

    dubbing_dir = out_dir / "dubbing"
    if dubbing_dir.exists():
        shutil.rmtree(dubbing_dir, ignore_errors=True)
        print("Removed dubbing directory")

    # Reset manifest stages from speech_asr onwards
    if manifest_path.exists():
        with open(manifest_path, "r", encoding="utf-8") as f:
            m_data = json.load(f)
        for stg in ["speech_asr", "visual_ocr", "ai_translation", "ai_tts", "audio_mixing", "video_rendering"]:
            m_data["stages"].pop(stg, None)
        m_data["current_stage"] = "speech_asr"
        m_data["status"] = "in_progress"
        m_data["tts_completed_segments"] = []
        with open(manifest_path, "w", encoding="utf-8") as f:
            json.dump(m_data, f, ensure_ascii=False, indent=2)
        print("Manifest reset to speech_asr.")

    orch = V1Orchestrator(workspace)
    manifest = orch.manifest_manager.load_manifest(job_id)
    video_path = manifest.video_path
    delivery = Path(r"D:\banve") / f"Dubbed_{Path(video_path).name}"

    print(f"\n[RE-PROCESS] Bắt đầu xử lý lại video: {Path(video_path).name}")
    print(f"[RE-PROCESS] Job ID: {job_id}")
    print(f"[RE-PROCESS] Thư mục output: {out_dir}")
    print(f"[RE-PROCESS] Thành phẩm sẽ lưu tại: {delivery}\n")

    async def on_progress(stage, step, total_steps, pct, msg, details):
        print(f"[{step}/{total_steps}] ({pct:.1f}%) {msg}")
        job_tracker.update_step(step, msg, percent=int(pct))

    res = await orch.execute_job(
        video_path=video_path,
        job_id=job_id,
        output_dir=out_dir,
        delivery_path=delivery,
        resume_if_possible=True,
        progress_callback=on_progress,
    )

    print("\n[SUCCESS] Xử lý hoàn tất!")
    print(f"Thành phẩm: {res.get('final_video')}")
    print(f"Quality Gate: {res.get('qc_status')}")

if __name__ == "__main__":
    asyncio.run(main())
