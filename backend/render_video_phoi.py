import asyncio
import os
import sys
import time
from pathlib import Path

# Fix Windows console UTF-8 encoding
if hasattr(sys.stdout, "reconfigure"):
    try: sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception: pass
if hasattr(sys.stderr, "reconfigure"):
    try: sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception: pass

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

async def process_video(video_path: Path):
    file_name = video_path.name
    base_name = video_path.stem
    
    workspace_dir = Path(
        os.getenv("AUTODUB_WORKSPACE", str(Path(BASE_DIR).parent / "workspace"))
    ).resolve()
    job_dir = workspace_dir / f"batch_{base_name}"
    job_dir.mkdir(parents=True, exist_ok=True)
    
    output_dir = Path(os.getenv("AUTODUB_OUTPUT_DIR", r"D:\banve")).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    final_dest = output_dir / f"Dubbed_{base_name}.mp4"
    
    settings = PipelineSettings.from_env()
    rvc_model = discover_rvc_model(workspace_dir)
    api_key = os.getenv("GEMINI_API_KEY", "")
    
    start_time = time.time()
    print(f"\n🎬 [Tool V2] Bắt đầu xử lý: {file_name}", flush=True)
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
    print(f"🎉 HOÀN TẤT {file_name} trong {total_time} giây ({total_time//60} phút {total_time%60} giây)!", flush=True)
    print(f"💾 Đã lưu thành phẩm vào: {final_dest}\n", flush=True)

async def main():
    input_folder = Path(os.getenv("AUTODUB_INPUT_DIR", r"D:\video phôi")).resolve()
    if not input_folder.exists():
        print(f"ERROR: Thư mục {input_folder} không tồn tại!", flush=True)
        return
        
    video_exts = (".mp4", ".mkv", ".mov", ".avi", ".webm", ".flv", ".m4v")
    videos = [p for p in input_folder.iterdir() if p.suffix.lower() in video_exts and not p.name.startswith("Dubbed_")]
    
    if not videos:
        print(f"Không tìm thấy video nào trong {input_folder}", flush=True)
        return
        
    print(f"📂 Tìm thấy {len(videos)} video trong {input_folder}:", flush=True)
    for v in videos:
        print(f"  - {v.name}", flush=True)
        
    for v in videos:
        await process_video(v)
        
    print("🚀 ĐÃ HOÀN THÀNH TẤT CẢ VIDEO TRONG THƯ MỤC 'video phôi'!", flush=True)

if __name__ == "__main__":
    asyncio.run(main())
