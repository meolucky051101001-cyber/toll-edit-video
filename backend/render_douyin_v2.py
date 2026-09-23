import asyncio
import os
import sys
import time
from pathlib import Path

# Fix Windows console UTF-8 encoding
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE_DIR)

env_file = os.path.join(BASE_DIR, ".env")
if os.path.exists(env_file):
    with open(env_file, "r", encoding="utf-8") as f:
        for line in f:
            if "=" in line and not line.strip().startswith("#"):
                k, v = line.strip().split("=", 1)
                os.environ[k.strip()] = v.strip().strip('"').strip("'")

from pipeline_v2.config import PipelineSettings
from pipeline_v2.video_pipeline import VideoPipelineRequest, VideoPipelineRunner, discover_rvc_model

async def main():
    video_path = Path(r"C:\tool v2\workspace\downloads\1789932323_c26d2025_douyin_7682719002690374963.mp4")
    if not video_path.exists():
        print(f"ERROR: Video file {video_path} not found!", flush=True)
        sys.exit(1)

    workspace_dir = Path(r"C:\tool v2\workspace")
    job_dir = workspace_dir / "job_douyin_7682719002690374963"
    job_dir.mkdir(parents=True, exist_ok=True)

    output_dir = Path(r"D:\video tool v2")
    output_dir.mkdir(parents=True, exist_ok=True)
    final_dest = output_dir / "Dubbed_1789932323_c26d2025_douyin_7682719002690374963.mp4"

    delivery_dir = Path(r"D:\banve")
    delivery_dir.mkdir(parents=True, exist_ok=True)
    delivery_dest = delivery_dir / "Dubbed_1789932323_c26d2025_douyin_7682719002690374963.mp4"

    settings = PipelineSettings.from_env()
    rvc_model = discover_rvc_model(Path(r"C:\tool v2\MyVoiceModel_v2"))
    if not rvc_model:
        rvc_model = discover_rvc_model(workspace_dir)

    api_key = os.getenv("GEMINI_API_KEY", "")

    start_time = time.time()
    print("=" * 70, flush=True)
    print("🚀 BẮT ĐẦU XỬ LÝ VIDEO DÀI 13.3 PHÚT BẰNG PIPELINE TOOL V2", flush=True)
    print("=" * 70, flush=True)
    print(f"🎬 Video nguồn: {video_path} ({video_path.stat().st_size / (1024*1024):.1f} MB)", flush=True)
    print(f"🎤 Model Giọng RVC: {rvc_model}", flush=True)
    print(f"📁 Thư mục xuất chính: {final_dest}", flush=True)
    print(f"📁 Thư mục giao nhận: {delivery_dest}", flush=True)
    print("=" * 70, flush=True)

    log_file = workspace_dir / "render_v2_status.log"

    def report_progress(stage, state):
        elapsed = int(time.time() - start_time)
        m, s = divmod(elapsed, 60)
        msg = f"⏱️ [{m:02d}:{s:02d}] [Tool V2] Stage: {stage} -> {state}"
        print(msg, flush=True)
        try:
            with open(log_file, "a", encoding="utf-8") as lf:
                lf.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}\n")
        except Exception:
            pass

    async def async_progress(stage, state):
        report_progress(stage, state)

    request = VideoPipelineRequest(
        video_path=video_path,
        job_directory=job_dir,
        output_path=final_dest,
        delivery_copy_path=delivery_dest,
        settings=settings,
        api_key=api_key,
        voice_source="rvc" if rvc_model else "edge",
        voice_param=str(rvc_model) if rvc_model else "vi-VN-HoaiMyNeural",
        rvc_model_path=rvc_model,
        progress=async_progress,
    )

    runner = VideoPipelineRunner(request)
    result = await runner.run()

    total_time = int(time.time() - start_time)
    m, s = divmod(total_time, 60)
    print("=" * 70, flush=True)
    print(f"🎉 HOÀN THÀNH XUẤT SẮC TOÀN BỘ PIPELINE V2 TRONG {m} PHÚT {s} GIÂY!", flush=True)
    print(f"💾 File thành phẩm Tool V2: {final_dest} (Tồn tại: {final_dest.exists()})", flush=True)
    print(f"💾 File sao lưu tại D:\\banve: {delivery_dest} (Tồn tại: {delivery_dest.exists()})", flush=True)
    print(f"📊 QC Report: {result.qc_report_path} (Allowed: {result.qc_allowed})", flush=True)
    print("=" * 70, flush=True)

if __name__ == "__main__":
    asyncio.run(main())
