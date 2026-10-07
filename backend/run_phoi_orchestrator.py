import asyncio
import os
import sys
import time
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

# Force Tool V1 legacy mode and avoid CUDA fragmentation
os.environ["PIPELINE_MODE"] = "legacy"
os.environ["PYTORCH_CUDA_ALLOC_CONF"] = "expandable_segments:True"

import torch
from batch_processor import process_single_local_video

def cleanup_memory():
    gc.collect()
    if torch.cuda.is_available():
        torch.cuda.empty_cache()

def is_telegram_bot_busy():
    """Kiểm tra xem bot Telegram có đang trong quá trình tải/OCR/RVC/render hay không."""
    app_log = Path(BASE_DIR) / "app.log"
    if not app_log.exists():
        return False
    try:
        with open(app_log, "r", encoding="utf-8", errors="ignore") as f:
            lines = f.readlines()
        for line in reversed(lines[-60:]):
            if any(k in line for k in ["Đang bóc tách", "Đang tải video", "Processing audio with", "Bắt đầu OCR", "Translating subtitles", "Applying RVC", "Mixing audio", "Processing final video"]):
                # Tìm thấy một công đoạn đang chạy, kiểm tra xem sau đó đã có log kết thúc chưa
                start_idx = lines.index(line)
                for after in lines[start_idx:]:
                    if any(fin in after for fin in ["Google Drive", "Render xong", "Hoàn tất", "Xử lý thất bại", "Worker task cancelled"]):
                        return False
                return True
    except Exception:
        pass
    return False

async def wait_for_bot_idle():
    print("🔍 [Orchestrator] Đang kiểm tra trạng thái Telegram bot...", flush=True)
    while is_telegram_bot_busy():
        print("⏳ [Orchestrator] Bot Telegram đang xử lý video được gửi qua link. Đang đợi bot hoàn tất...", flush=True)
        await asyncio.sleep(6)
    print("✅ [Orchestrator] Bot Telegram đã hoàn tất và đang ở trạng thái rảnh rỗi!", flush=True)
    print("⏳ [Orchestrator] Đợi 5 giây để ổn định bộ nhớ VRAM...", flush=True)
    cleanup_memory()
    await asyncio.sleep(5)

async def main():
    cleanup_memory()
    await wait_for_bot_idle()
    
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
        
    print(f"\n🎬 [Tool V1] Tìm thấy {len(video_files)} video phôi trong {input_dir}:", flush=True)
    for vf in video_files:
        print(f" - {vf.name}", flush=True)
        
    for idx, vf in enumerate(video_files, 1):
        cleanup_memory()
        dest_check = list(output_dir.glob(f"*{vf.stem}*.mp4"))
        if dest_check:
            print(f"\n⏩ [{idx}/{len(video_files)}] Video {vf.name} đã được render tại {dest_check[0].name}. Bỏ qua...", flush=True)
            continue
            
        print(f"\n==========================================", flush=True)
        print(f"🚀 [{idx}/{len(video_files)}] Bắt đầu xử lý video phôi: {vf.name}", flush=True)
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

    print("\n🏁 [Orchestrator] Hoàn tất toàn bộ video phôi!", flush=True)

if __name__ == "__main__":
    asyncio.run(main())
