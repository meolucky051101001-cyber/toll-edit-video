import asyncio
import datetime
import os
import sys
import types
from pathlib import Path

# Ensure UTF-8 output on Windows
sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

import srt
from pydub import AudioSegment
from backend.ai.voice_cloning import (
    calculate_reading_windows,
    fit_audio_file,
    generate_single_tts,
    MAX_NATURAL_SPEED,
)
from backend.ass_utils import sync_and_clamp_subtitles
from backend.v1_audio_mixer import mix_adaptive_audio


async def run_all_voice_tests():
    print("=" * 70)
    print("TEST TOÀN DIỆN TÍNH NĂNG NHANH CHẬM & CHỐNG ĐÈ CHO TẤT CẢ CÁC GIỌNG")
    print("=" * 70)

    test_dir = Path(__file__).resolve().parents[1] / "workspace" / "test_voices_anti_overlap"
    test_dir.mkdir(parents=True, exist_ok=True)

    # 1. Kịch bản test đa câu liền kề sát nhau (cự ly hẹp, câu dài câu ngắn)
    # Câu 1: Bắt đầu 0.5s, kết thúc 2.2s (khoảng đọc ~ 1.65s). Nội dung dài -> cần nói nhanh / rút gọn.
    # Câu 2: Bắt đầu 2.2s, kết thúc 4.5s (khoảng đọc ~ 2.25s). Nội dung vừa.
    # Câu 3: Bắt đầu 4.5s, kết thúc 7.0s (khoảng đọc ~ 2.45s). Nội dung ngắn -> thư giãn nhịp đọc.
    segments = [
        srt.Subtitle(
            index=1,
            start=datetime.timedelta(seconds=0.5),
            end=datetime.timedelta(seconds=2.2),
            content="Xin chào các bạn đã quay trở lại kênh của mình trong video ngày hôm nay.",
        ),
        srt.Subtitle(
            index=2,
            start=datetime.timedelta(seconds=2.2),
            end=datetime.timedelta(seconds=4.5),
            content="Chiếc máy này có thiết kế vô cùng nhỏ gọn và tinh tế.",
        ),
        srt.Subtitle(
            index=3,
            start=datetime.timedelta(seconds=4.5),
            end=datetime.timedelta(seconds=7.0),
            content="Thật tuyệt vời!",
        ),
    ]

    # Tính khoảng đọc tối đa cho từng câu (Điểm 1)
    windows = calculate_reading_windows(segments, video_duration=8.0, gap_s=0.05)
    print("\n1. Khoảng đọc tối đa (Reading Windows) an toàn:")
    for s in segments:
        w = windows[s.index]
        print(f"   - Câu #{s.index}: [{s.start.total_seconds():.2f}s - {s.end.total_seconds():.2f}s] -> Hard Max = {w:.2f}s")
        assert w > 0, f"Window for segment {s.index} must be positive"

    # Danh sách các giọng khác cần kiểm tra (CapCut Nam BV075, CapCut Nữ BV421, Edge TTS Hoài My)
    voices_to_test = [
        {"id": "capcut-BV075_streaming", "source": "capcut", "param": "BV075_streaming", "name": "Thanh Niên Tự Tin (CapCut Nam)"},
        {"id": "microsoft-hoaimy", "source": "edge", "param": "vi-VN-HoaiMyNeural", "name": "Hoài My (Edge TTS Nữ)"},
    ]

    for v_info in voices_to_test:
        v_name = v_info["name"]
        v_source = v_info["source"]
        v_param = v_info["param"]
        v_out_dir = test_dir / v_info["id"]
        v_out_dir.mkdir(parents=True, exist_ok=True)

        print(f"\n" + "-" * 60)
        print(f"KIỂM TRA GIỌNG: {v_name} ({v_source} : {v_param})")
        print("-" * 60)

        results = []
        for s in segments:
            hard_max = windows[s.index]
            res = await generate_single_tts(
                s,
                str(v_out_dir),
                voice_source=v_source,
                voice_param=v_param,
                api_key="",
                target_max_duration=hard_max,
            )
            assert res is not None, f"TTS thất bại cho câu #{s.index} với giọng {v_name}"
            actual_dur = res["actual_audio_duration"]
            speed_ratio = res["speed_ratio"]
            print(f"   Câu #{s.index}: Text='{res['content']}'")
            print(f"      Thời lượng thực tế: {actual_dur:.2f}s (Mục tiêu tối đa: {hard_max:.2f}s) | Tỉ lệ tốc độ: {speed_ratio:.2f}x")
            
            # Kiểm tra thời lượng thực tế không được vượt quá khoảng đọc cho phép (+ dung sai 0.05s)
            assert actual_dur <= hard_max + 0.05, f"Audio câu #{s.index} ({actual_dur:.2f}s) vượt quá mốc an toàn {hard_max:.2f}s"
            results.append(res)

        # Kiểm tra tính toán Overlap giữa các câu liền kề
        print(f"\n   -> Kiểm tra khoảng cách / đè giọng giữa các câu của {v_name}:")
        for i in range(len(results) - 1):
            cur_dub = results[i]
            next_dub = results[i + 1]
            cur_start = cur_dub["start"]
            cur_dur = cur_dub["actual_audio_duration"]
            cur_end = cur_start + cur_dur
            next_start = next_dub["start"]
            
            diff = cur_end - next_start
            status = f"❌ ĐÈ {diff:+.3f}s" if diff > 0.001 else f"✅ Cách {abs(diff):.3f}s (An toàn)"
            print(f"      Câu #{cur_dub['index']} ({cur_end:.3f}s) -> Câu #{next_dub['index']} ({next_start:.3f}s): {status}")
            assert diff <= 0.005, f"Bị đè giọng giữa câu #{cur_dub['index']} và #{next_dub['index']}: {diff:+.3f}s"

        # Kiểm tra đồng bộ phụ đề (Subtitle Sync & Anti-Overlap)
        synced_segs = sync_and_clamp_subtitles(segments, results)
        for i in range(len(synced_segs) - 1):
            c_seg = synced_segs[i]
            n_seg = synced_segs[i + 1]
            assert c_seg.end <= n_seg.start, f"Phụ đề câu #{c_seg.index} đè lên phụ đề câu #{n_seg.index}"
        print(f"   -> Phụ đề đã đồng bộ chuẩn xác và không đè nhau.")

        # Kiểm tra mixer hòa âm an toàn (Mixer Hard Guard)
        silent_bgm = AudioSegment.silent(duration=8000)
        bgm_path = str(v_out_dir / "test_bgm.wav")
        silent_bgm.export(bgm_path, format="wav").close()
        mixed_path = str(v_out_dir / "test_mixed.wav")
        mix_adaptive_audio(bgm_path, results, mixed_path)
        assert os.path.exists(mixed_path) and os.path.getsize(mixed_path) > 1000, "File mixed audio không tồn tại"
        print(f"   -> Mixer hòa âm an toàn thành công với Hard Guard.")

    print("\n" + "=" * 70)
    print("HOÀN TẤT 100%: TẤT CẢ CÁC GIỌNG ĐỀU ĐẠT CHUẨN TỐC ĐỘ VÀ CHỐNG ĐÈ GIỐNG CHÍ MAI!")
    print("=" * 70)


if __name__ == "__main__":
    asyncio.run(run_all_voice_tests())
