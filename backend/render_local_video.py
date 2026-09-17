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

async def run():
    video_path = Path(r"D:\1788274800377_video.mp4")
    if not video_path.exists():
        print(f"ERROR: Video {video_path} not found!", flush=True)
        return
        
    workspace_dir = Path(r"C:\Users\admin\.gemini\antigravity\scratch\video-dubbing-app\workspace")
    job_dir = workspace_dir / "batch_1788274800377_video"
    job_dir.mkdir(parents=True, exist_ok=True)
    
    output_dir = Path(r"D:\banve")
    output_dir.mkdir(parents=True, exist_ok=True)
    final_dest = output_dir / "Dubbed_1788274800377_video.mp4"
    
    settings = PipelineSettings.from_env()
    rvc_model = discover_rvc_model(workspace_dir)
    api_key = os.getenv("GEMINI_API_KEY", "")
    
    start_time = time.time()
    print(f"🎬 Bắt đầu render video cục bộ {video_path} (21 phút 10 giây) bằng Tool V2...", flush=True)
    print(f"🎤 RVC Voice Model: {rvc_model}", flush=True)
    print(f"💾 File xuất: {final_dest}", flush=True)
    
    async def progress(stage, state):
        elapsed = int(time.time() - start_time)
        print(f"⏱️ [{elapsed}s] [Pipeline V2] {stage}: {state}", flush=True)
        
    request = VideoPipelineRequest(
        video_path=video_path,
        job_directory=job_dir,
        output_path=final_dest,
        settings=settings,
        api_key=api_key,
        voice_source="rvc" if rvc_model else "edge",
        voice_param=str(rvc_model) if rvc_model else "vi-VN-HoaiMyNeural",
        rvc_model_path=rvc_model,
        progress=progress,
    )
    
    runner = VideoPipelineRunner(request)
    result = await runner.run()
    total_time = int(time.time() - start_time)
    print(f"🎉 HOÀN TẤT RENDER VIDEO 21 PHÚT trong {total_time} giây ({total_time//60} phút {total_time%60} giây)!", flush=True)
    print(f"💾 Đã lưu thành phẩm vào: {final_dest}", flush=True)

if __name__ == "__main__":
    asyncio.run(run())
