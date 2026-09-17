import asyncio
import os
import sys
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

# Force Tool V1 legacy mode
os.environ["PIPELINE_MODE"] = "legacy"

from batch_processor import process_single_local_video

async def main():
    video_path = r"D:\video phôi\1788280024245_video.mp4"
    output_dir = r"D:\banve"
    print(f"🎬 [Tool V1] Bắt đầu xử lý: {video_path}", flush=True)
    
    async def progress(msg):
        print(f"⏱️ [Tool V1] {msg}", flush=True)
        
    success = await process_single_local_video(video_path, output_dir, progress)
    if success:
        print(f"🎉 [Tool V1] HOÀN TẤT THÀNH CÔNG! Đã lưu vào {output_dir}", flush=True)
    else:
        print(f"❌ [Tool V1] Thất bại!", flush=True)

if __name__ == "__main__":
    asyncio.run(main())
