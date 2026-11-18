import asyncio
import os
import sys
from pathlib import Path

# Ensure UTF-8 output
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
    orch = V1Orchestrator(workspace)
    jid = "tg_1791024189_1eb3e3d8_改画改画你们画我来改_第32期_1791024275"
    manifest = orch.manifest_manager.load_manifest(jid)
    if not manifest:
        print(f"[ERROR] Không tìm thấy manifest cho job: {jid}")
        return

    video_path = manifest.video_path
    out_dir = Path(manifest.effective_config["output_dir"])
    delivery = Path(r"D:\banve") / f"Dubbed_{Path(video_path).name}"

    print(f"[RESUME] Bắt đầu tiếp tục xử lý video: {Path(video_path).name}")
    print(f"[RESUME] Job ID: {jid}")
    print(f"[RESUME] Thư mục output: {out_dir}")
    print(f"[RESUME] Thành phẩm sẽ lưu tại: {delivery}")

    async def on_progress(stage, step, total_steps, pct, msg, details):
        print(f"[{step}/{total_steps}] ({pct:.1f}%) {msg}")
        job_tracker.update_step(step, msg, percent=int(pct))

    res = await orch.execute_job(
        video_path=video_path,
        job_id=jid,
        output_dir=out_dir,
        delivery_path=delivery,
        resume_if_possible=True,
        progress_callback=on_progress,
    )

    print("[SUCCESS] Xử lý hoàn tất!")
    print(f"Thành phẩm: {res.get('final_video')}")
    print(f"Quality Gate: {res.get('qc_status')}")

if __name__ == "__main__":
    asyncio.run(main())
