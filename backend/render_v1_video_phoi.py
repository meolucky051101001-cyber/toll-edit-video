import asyncio
import os
import sys
import gc
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    try: sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception: pass
if hasattr(sys.stderr, "reconfigure"):
    try: sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception: pass

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE_DIR)

# Force Tool V1 legacy mode and avoid CUDA memory fragmentation
os.environ["PIPELINE_MODE"] = "legacy"
os.environ["PYTORCH_CUDA_ALLOC_CONF"] = "expandable_segments:True"

import torch
from batch_processor import process_single_local_video

def cleanup_memory():
    gc.collect()
    if torch.cuda.is_available():
        torch.cuda.empty_cache()

async def main():
    cleanup_memory()
    input_dir = Path("D:/video phôi")
    if not input_dir.exists():
        input_dir = Path("D:/video phoi")
    output_dir = Path(r"D:\banve")
    output_dir.mkdir(parents=True, exist_ok=True)
    
    video_files = []
    for f in sorted(input_dir.glob("*.mp4")):
        if " (1)" in f.name:
            continue
        video_files.append(f)
        
    print(f"🎬 [Tool V1] Tìm thấy {len(video_files)} video phôi trong {input_dir}:", flush=True)
    for vf in video_files:
        print(f" - {vf.name}", flush=True)
        
    for idx, vf in enumerate(video_files, 1):
        cleanup_memory()
        dest_check = list(output_dir.glob(f"*{vf.stem}*.mp4"))
        if dest_check:
            print(f"\n⏩ [{idx}/{len(video_files)}] Video {vf.name} đã được render tại {dest_check[0].name}. Tiếp tục video tiếp theo...", flush=True)
            continue
            
        print(f"\n==========================================", flush=True)
        print(f"🚀 [{idx}/{len(video_files)}] Bắt đầu xử lý: {vf.name}", flush=True)
        print(f"==========================================", flush=True)
        
        async def progress(msg):
            print(f"⏱️ [{vf.name}] {msg}", flush=True)
            
        try:
            success = await process_single_local_video(str(vf), str(output_dir), progress,
                                                       queue_index=idx, queue_total=len(video_files))
            if success:
                print(f"🎉 [{idx}/{len(video_files)}] HOÀN TẤT THÀNH CÔNG: {vf.name} -> {output_dir}", flush=True)
            else:
                print(f"❌ [{idx}/{len(video_files)}] Xử lý thất bại: {vf.name}", flush=True)
        except Exception as exc:
            print(f"❌ [{idx}/{len(video_files)}] Lỗi ngoại lệ: {exc}", flush=True)
        finally:
            cleanup_memory()

    print("\n🏁 [Tool V1] Hoàn tất toàn bộ hàng đợi video phôi!", flush=True)

if __name__ == "__main__":
    asyncio.run(main())
