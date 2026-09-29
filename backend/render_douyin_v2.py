import asyncio
import argparse
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

from environment import load_environment

load_environment(Path(BASE_DIR))

try:
    from config.paths import AppPaths
except ImportError:
    from backend.config.paths import AppPaths

PATHS = AppPaths.from_environment(Path(BASE_DIR).parent)

from pipeline_v2.config import PipelineSettings
from pipeline_v2.video_pipeline import VideoPipelineRequest, VideoPipelineRunner, discover_rvc_model

async def main(video_path: Path):
    if not video_path.exists():
        print(f"ERROR: Video file {video_path} not found!", flush=True)
        sys.exit(1)

    workspace_dir = PATHS.workspace
    workspace_dir.mkdir(parents=True, exist_ok=True)
    job_dir = workspace_dir / f"job_{video_path.stem}"
    job_dir.mkdir(parents=True, exist_ok=True)

    output_dir = PATHS.output_dir
    output_dir.mkdir(parents=True, exist_ok=True)
    final_dest = output_dir / f"Dubbed_{video_path.stem}.mp4"

    delivery_path = os.getenv("AUTODUB_DELIVERY_DIR", "").strip()
    delivery_dest = None
    if delivery_path:
        delivery_dir = Path(delivery_path).expanduser()
        if not delivery_dir.is_absolute():
            delivery_dir = PATHS.project_root / delivery_dir
        delivery_dir.mkdir(parents=True, exist_ok=True)
        delivery_dest = delivery_dir / final_dest.name

    settings = PipelineSettings.from_env()
    rvc_model = discover_rvc_model(Path(BASE_DIR) / "MyVoiceModel_v2")
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
    if delivery_dest:
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
    if delivery_dest:
        print(f"💾 File sao lưu: {delivery_dest} (Tồn tại: {delivery_dest.exists()})", flush=True)
    print(f"📊 QC Report: {result.qc_report_path} (Allowed: {result.qc_allowed})", flush=True)
    print("=" * 70, flush=True)

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Chạy thử một video bằng Pipeline V2.")
    parser.add_argument("video", type=Path, help="Đường dẫn video nguồn cần chạy thử")
    args = parser.parse_args()
    asyncio.run(main(args.video.expanduser().resolve()))
