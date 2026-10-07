# -*- coding: utf-8 -*-
"""Generate preview audio and video clips for all 14 VieNeu voices in tool_control."""

import os
import sys
import subprocess
import tempfile
from pathlib import Path

backend_dir = Path(__file__).resolve().parent
sys.path.insert(0, str(backend_dir))
if sys.stdout.encoding != 'utf-8':
    sys.stdout.reconfigure(encoding='utf-8')

from ai.vieneu_tts_service import generate_tts_vieneu_sync

TEXT = "Xin chào, đây là giọng đọc thử. Hôm nay chúng ta cùng khám phá những món đồ thú vị."

VOICES = [
    ("vieneu-haidang", "Hải Đăng"),
    ("vieneu-maianh", "Mai Anh"),
    ("vieneu-trucly", "Trúc Ly"),
    ("vieneu-thienminh", "Thiện Minh"),
    ("vieneu-thuydung", "Thùy Dung"),
    ("vieneu-adambua", "Adam bựa"),
    ("vieneu-thaison", "Thái Sơn"),
    ("vieneu-phamtuyen", "Phạm Tuyên"),
    ("vieneu-ngochuyen", "Ngọc Huyền"),
    ("vieneu-ngoctran", "Ngọc Trân"),
    ("vieneu-quangson", "Quang Sơn"),
    ("vieneu-myduyen", "Mỹ Duyên"),
    ("vieneu-quynhanh", "Quỳnh Anh"),
    ("vieneu-thanhbinh", "Thanh Bình"),
]

# Destination folders
TARGET_DIRS = [
    (Path(r"C:\tool v1\workspace\bot_system\control\voice_checks"), Path(r"C:\tool v1\workspace\bot_system\control\voice_previews")),
    (Path(r"C:\tool v1\workspace\control\voice_checks"), Path(r"C:\tool v1\workspace\control\voice_previews")),
    (Path(r"C:\tool v2\workspace\control\voice_checks"), Path(r"C:\tool v2\workspace\control\voice_previews")),
]

for checks_dir, previews_dir in TARGET_DIRS:
    checks_dir.mkdir(parents=True, exist_ok=True)
    previews_dir.mkdir(parents=True, exist_ok=True)

sample_video = Path(r"C:\tool v1\workspace\bot_system\control\sample_test_video.mp4")
if not sample_video.is_file():
    sample_video = Path(r"C:\tool v1\workspace\control\sample_test_video.mp4")

print(f"Sample video found: {sample_video} (exists: {sample_video.is_file()})")

def main():
    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp_path = Path(tmp_dir)
        for idx, (voice_id, voice_param) in enumerate(VOICES):
            print(f"[{idx+1}/{len(VOICES)}] Generating preview for {voice_id} ({voice_param})...")
            wav_path = tmp_path / f"{voice_id}.wav"
            mp3_path = tmp_path / f"{voice_id}.mp3"
            mp4_path = tmp_path / f"{voice_id}.mp4"

            # 1. Synthesize audio with VieNeu
            generate_tts_vieneu_sync(
                text=TEXT,
                voice=voice_param,
                output_path=wav_path
            )

            # 2. Convert to MP3
            subprocess.run([
                "ffmpeg", "-y", "-i", str(wav_path),
                "-codec:a", "libmp3lame", "-qscale:a", "2",
                str(mp3_path)
            ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)

            # 3. Create preview MP4 merged with sample video (padded to 14s)
            if sample_video.is_file():
                subprocess.run([
                    "ffmpeg", "-y",
                    "-ss", "0", "-t", "14.0", "-i", str(sample_video),
                    "-i", str(wav_path),
                    "-filter_complex", "[1:a]apad=whole_dur=14.0[a]",
                    "-map", "0:v", "-map", "[a]",
                    "-c:v", "copy",
                    "-c:a", "aac", "-b:a", "192k",
                    "-shortest",
                    str(mp4_path)
                ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)

            # 4. Copy to all target directories
            for checks_dir, previews_dir in TARGET_DIRS:
                if mp3_path.is_file():
                    dest_mp3 = checks_dir / f"{voice_id}.mp3"
                    dest_mp3.write_bytes(mp3_path.read_bytes())
                if wav_path.is_file():
                    dest_wav = checks_dir / f"{voice_id}.wav"
                    dest_wav.write_bytes(wav_path.read_bytes())
                if mp4_path.is_file():
                    dest_mp4 = previews_dir / f"{voice_id}.mp4"
                    dest_mp4.write_bytes(mp4_path.read_bytes())

            print(f"  -> Done {voice_id} (MP3: {mp3_path.stat().st_size:,}B, MP4: {mp4_path.stat().st_size:,}B)")

    print("\n✅ All 14 VieNeu preview clips generated successfully!")

if __name__ == "__main__":
    main()
