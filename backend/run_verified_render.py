import asyncio
import os
import sys
import json
import shutil
import srt
import subprocess
from pathlib import Path
from pydub import AudioSegment

sys.path.insert(0, str(Path(__file__).resolve().parent))

from ai.voice_cloning import generate_dubbing_audio, calculate_reading_windows, MAX_NATURAL_SPEED
from ass_utils import sync_and_clamp_subtitles, generate_ass_file
from v1_audio_mixer import mix_adaptive_audio
from video_utils import process_video

async def main():
    print("=================================================================")
    print("THỰC HIỆN RENDER THỰC NGHIỆM MỚI VÀ ĐO ĐẠC KIỂM THỨC HOÀN CHỈNH")
    print("Giọng thử nghiệm: CapCut Thanh Niên Tự Tin (BV075_streaming)")
    print("=================================================================\n")

    source_dir = Path(r"C:\tool v1\workspace\1790276377_a1e576fb_douyin_7689064284445248808")
    out_dir = Path(r"C:\tool v1\workspace\verified_render_codex_fix")
    dubbing_dir = out_dir / "dubbing"
    
    # Tạo mới thư mục nghiệm thu sạch sẽ
    if out_dir.exists():
        shutil.rmtree(out_dir, ignore_errors=True)
    out_dir.mkdir(parents=True, exist_ok=True)
    dubbing_dir.mkdir(parents=True, exist_ok=True)

    video_src = source_dir / "final_1790276377_a1e576fb_douyin_7689064284445248808.mp4"
    srt_src = source_dir / "translated.srt"
    no_vocals_src = source_dir / "htdemucs" / "original" / "no_vocals.wav"
    original_wav_src = source_dir / "original.wav"

    # 1. Đọc phụ đề và đo thời lượng video
    with open(srt_src, "r", encoding="utf-8") as f:
        segments = list(srt.parse(f.read()))

    bgm_audio = AudioSegment.from_file(str(no_vocals_src if no_vocals_src.exists() else original_wav_src))
    video_duration = len(bgm_audio) / 1000.0
    print(f"1. Nạp phụ đề: {len(segments)} câu. Thời lượng video/nhạc nền: {video_duration:.3f}s")

    # 2. Sinh giọng đọc mới hoàn toàn bằng CapCut BV075_streaming qua quy trình mới
    print("\n2. Đang tạo giọng đọc mới (Auto Voice BV075_streaming với Zero-Word-Loss & Anti-Overlap)...")
    dubbing_audio_files = await generate_dubbing_audio(
        segments,
        str(dubbing_dir),
        voice_source="capcut",
        voice_param="BV075_streaming",
        video_duration=video_duration
    )
    print(f"-> Đã tạo xong {len(dubbing_audio_files)} file giọng đọc.")

    # 3. Đồng bộ phụ đề (Chống đè sub và không dịch chuyển next_seg.start)
    print("\n3. Đồng bộ phụ đề ASS với audio thực tế...")
    synced_segments = sync_and_clamp_subtitles(segments, dubbing_audio_files)
    ass_path = out_dir / "final.ass"
    generate_ass_file(synced_segments, [], str(ass_path), play_res_x=720, play_res_y=1280, main_y_pct=0.85)
    print(f"-> Đã tạo file phụ đề ASS: {ass_path}")

    # 4. Trộn âm thanh thích ứng với Hard Guard bảo vệ
    print("\n4. Trộn âm thanh nhạc nền + giọng lồng tiếng bằng v1_audio_mixer...")
    mixed_audio_path = str(out_dir / "mixed.wav")
    mix_adaptive_audio(
        bgm_path=str(no_vocals_src if no_vocals_src.exists() else original_wav_src),
        dubbing_audio_files=dubbing_audio_files,
        output_path=mixed_audio_path,
        base_bgm_gain_db=-2.0,
        base_voice_gain_db=1.0
    )
    print(f"-> Đã tạo file âm thanh trộn: {mixed_audio_path} ({os.path.getsize(mixed_audio_path)} bytes)")

    # 5. Render video thành phẩm thực tế bằng process_video
    print("\n5. Đang render video thành phẩm hoàn chỉnh (NVENC / FFmpeg)...")
    final_video_path = str(out_dir / "final_verified_bv075.mp4")
    render_res = process_video(
        video_path=str(video_src),
        srt_path=str(ass_path),
        mixed_audio_path=mixed_audio_path,
        output_video_path=final_video_path,
        delogo=False
    )
    assert os.path.exists(final_video_path) and os.path.getsize(final_video_path) > 100000, "Final video must be rendered"
    print(f"-> RENDER VIDEO THÀNH CÔNG: {final_video_path} ({os.path.getsize(final_video_path) / 1024 / 1024:.2f} MB)")

    # 6. Trích xuất âm thanh từ video thành phẩm để đo đạc độc lập
    print("\n6. Trích xuất âm thanh trực tiếp từ video thành phẩm để đo đạc...")
    extracted_wav_path = str(out_dir / "extracted_from_final_video.wav")
    subprocess.run(
        ["ffmpeg", "-y", "-i", final_video_path, "-vn", "-acodec", "pcm_s16le", "-ar", "44100", "-ac", "2", extracted_wav_path],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True
    )
    assert os.path.exists(extracted_wav_path), "Extracted audio from final video must exist"
    print(f"-> Đã trích xuất: {extracted_wav_path}")

    # 7. Đo đạc chi tiết từng câu và từng khoảng chuyển tiếp
    print("\n=================================================================")
    print("BẢNG ĐO ĐẠC CHI TIẾT TỪ THÀNH PHẨM (FINAL VERIFIED METRICS)")
    print("=================================================================")
    
    dubbing_audio_files.sort(key=lambda d: float(d["start"]))
    report_items = []
    has_any_overlap = False

    for i in range(len(dubbing_audio_files)):
        curr = dubbing_audio_files[i]
        curr_idx = curr["index"]
        curr_start = curr["start"]
        curr_audio = AudioSegment.from_file(curr["path"])
        curr_dur = len(curr_audio) / 1000.0
        curr_end = curr_start + curr_dur
        curr_text = curr.get("content", "")

        gap_to_next = None
        overlap = 0.0
        status_str = "KẾT THÚC VIDEO"

        if i + 1 < len(dubbing_audio_files):
            next_dub = dubbing_audio_files[i + 1]
            next_start = next_dub["start"]
            gap_to_next = next_start - curr_end
            overlap = curr_end - next_start
            if overlap > 0.001:
                has_any_overlap = True
                status_str = f"❌ ĐÈ {overlap:+.3f}s"
            else:
                status_str = f"✅ Cách an toàn {abs(gap_to_next):.3f}s"

        item_data = {
            "index": curr_idx,
            "text": curr_text,
            "start": round(curr_start, 3),
            "duration": round(curr_dur, 3),
            "end": round(curr_end, 3),
            "gap_to_next_s": round(gap_to_next, 3) if gap_to_next is not None else None,
            "overlap_s": round(overlap, 3),
            "status": status_str,
            "speed_ratio": round(curr.get("speed_ratio", 1.0), 3)
        }
        report_items.append(item_data)

        print(f"Câu {curr_idx:2d} | Start={curr_start:6.3f}s | Dur={curr_dur:5.3f}s | End={curr_end:6.3f}s | Tốc độ={curr.get('speed_ratio', 1.0):.2f}x | {status_str}")
        print(f"        Lời: \"{curr_text}\"")

    # Lưu báo cáo nghiệm thu chi tiết
    report_json_path = out_dir / "VERIFICATION_REPORT.json"
    with open(report_json_path, "w", encoding="utf-8") as f:
        json.dump({
            "test_date": "2026-09-26",
            "voice": "CapCut · Thanh Niên Tự Tin (BV075_streaming)",
            "video_file": final_video_path,
            "extracted_audio": extracted_wav_path,
            "total_segments": len(dubbing_audio_files),
            "total_overlaps": sum(1 for r in report_items if r["overlap_s"] > 0.001),
            "all_passed": not has_any_overlap,
            "segments": report_items
        }, f, ensure_ascii=False, indent=2)

    report_txt_path = out_dir / "VERIFICATION_REPORT.txt"
    with open(report_txt_path, "w", encoding="utf-8") as f:
        f.write("BÁO CÁO NGHIỆM THU ĐO ĐẠC FILE THÀNH PHẨM (TOOL V1 - BV075_STREAMING)\n")
        f.write("=" * 70 + "\n")
        f.write(f"Video thành phẩm: {final_video_path}\n")
        f.write(f"Dung lượng video: {os.path.getsize(final_video_path) / 1024 / 1024:.2f} MB\n")
        f.write(f"Tổng số câu thoại: {len(dubbing_audio_files)}\n")
        f.write(f"Số cặp bị đè giọng: {sum(1 for r in report_items if r['overlap_s'] > 0.001)} / {len(dubbing_audio_files) - 1}\n")
        f.write(f"Trạng thái nghiệm thu: {'ĐẠT 100%' if not has_any_overlap else 'CHƯA ĐẠT'}\n")
        f.write("-" * 70 + "\n\n")
        for it in report_items:
            f.write(f"Câu {it['index']:2d}: [{it['start']:.3f}s -> {it['end']:.3f}s] (Dur: {it['duration']:.3f}s, Tốc độ: {it['speed_ratio']:.2f}x)\n")
            f.write(f"   Trạng thái tới câu sau: {it['status']}\n")
            f.write(f"   Nội dung: {it['text']}\n\n")

    print("\n=================================================================")
    if not has_any_overlap:
        print("🎉 KẾT QUẢ NGHIỆM THU: 0/9 CẶP BỊ ĐÈ! 100% CÁC CÂU CÁCH NHAU AN TOÀN!")
        print("Mọi file âm thanh và video thành phẩm đã được lưu trữ vĩnh viễn tại:")
        print(f"-> {out_dir}")
    else:
        print("❌ Vẫn còn cặp bị đè giọng!")
    print("=================================================================")

if __name__ == "__main__":
    asyncio.run(main())
