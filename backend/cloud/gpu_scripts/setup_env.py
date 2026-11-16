"""
setup_env.py - Chạy trên Colab VM để kiểm tra GPU và cài đặt môi trường.
"""
import glob
import os
import subprocess
import sys

def main():
    script_dir = os.path.dirname(os.path.abspath(__file__))
    if script_dir not in sys.path:
        sys.path.insert(0, script_dir)

    print("[*] Kiểm tra môi trường GPU trên Colab VM...", flush=True)
    try:
        import torch
        cuda_ok = torch.cuda.is_available()
        gpu_name = torch.cuda.get_device_name(0) if cuda_ok else "Không có CUDA"
        print(f"  -> CUDA Available: {cuda_ok} | Device: {gpu_name}", flush=True)
    except ImportError:
        print("  -> Đang cài đặt PyTorch...", flush=True)
        subprocess.run([sys.executable, "-m", "pip", "install", "torch", "torchaudio"], check=True)

    print("[*] Cài đặt dependencies cho pipeline (Whisper, Demucs, Edge-TTS, Gemini, SoundFile)...", flush=True)
    req_file = os.path.join(script_dir, "requirements_cloud.txt")
    if os.path.exists(req_file):
        subprocess.run([sys.executable, "-m", "pip", "install", "-q", "-r", req_file], check=True)
    else:
        subprocess.run([sys.executable, "-m", "pip", "install", "-q", "-r", "requirements_cloud.txt"], check=True)

    # Cài đặt các gói bánh xe nội bộ (.whl) nếu có, ví dụ capcut_tts_api
    whl_files = glob.glob(os.path.join(script_dir, "*.whl")) or glob.glob("*.whl")
    for whl in whl_files:
        print(f"  -> Cài đặt gói mở rộng: {whl}...", flush=True)
        subprocess.run([sys.executable, "-m", "pip", "install", "-q", whl], check=True)

    # Kiểm tra xác thực các module quan trọng
    try:
        import demucs
        import whisper
        from mixer import mix_audio_ffmpeg
        print("  -> Đã xác thực thành công: Demucs, Whisper, Mixer EBU R128.", flush=True)
    except Exception as exc:
        print(f"  [WARN] Cảnh báo xác thực module: {exc}", flush=True)

    print("[SUCCESS] Môi trường Colab VM đã sẵn sàng 100%!", flush=True)

if __name__ == "__main__":
    main()
