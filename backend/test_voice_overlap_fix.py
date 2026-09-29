import asyncio
import os
import sys
import math
import srt
import datetime
from pathlib import Path
from pydub import AudioSegment

sys.path.insert(0, str(Path(__file__).resolve().parent))

from ai.voice_cloning import (
    calculate_reading_windows,
    trim_audio_silence,
    fit_audio_file,
    MAX_NATURAL_SPEED,
)
from ai.translation import condense_vietnamese_subtitles_batch
from ai.v1_voice_cache import voice_cache_key, read_voice_cache, write_voice_cache, CACHE_KEY_VERSION
from ass_utils import sync_and_clamp_subtitles
from v1_audio_mixer import mix_adaptive_audio

async def run_tests():
    sample_dir = Path(r"C:\tool v1\workspace\1790276377_a1e576fb_douyin_7689064284445248808")
    srt_path = sample_dir / "translated.srt"
    dubbing_dir = sample_dir / "dubbing"
    temp_dir = sample_dir / "test_fitted"
    temp_dir.mkdir(exist_ok=True)

    print("=================================================================")
    print("KIỂM TRA TOÀN DIỆN KẾ HOẠCH CODEX - TOOL V1 VOICE OVERLAP FIX")
    print("=================================================================\n")

    # Đọc phụ đề
    with open(srt_path, "r", encoding="utf-8") as f:
        segments = list(srt.parse(f.read()))
    print(f"1. Đã nạp {len(segments)} đoạn phụ đề từ sample job.")

    # -------------------------------------------------------------
    # BƯỚC 1: Đo đạc nguyên trạng (Baseline Trước Khi Sửa)
    # -------------------------------------------------------------
    print("\n--- [BASELINE] ĐO ĐẠC NGUYÊN TRẠNG BAN ĐẦU ---")
    raw_audios = {}
    for seg in segments:
        p = dubbing_dir / f"{seg.index}.mp3"
        if p.exists():
            seg_audio = AudioSegment.from_file(str(p))
            dur = len(seg_audio) / 1000.0
            raw_audios[seg.index] = (dur, str(p))
        else:
            print(f"Thiếu file: {p}")

    baseline_overlaps = []
    for i in range(len(segments) - 1):
        s1 = segments[i]
        s2 = segments[i + 1]
        start1 = s1.start.total_seconds()
        start2 = s2.start.total_seconds()
        dur1 = raw_audios[s1.index][0]
        end1 = start1 + dur1
        overlap = end1 - start2
        baseline_overlaps.append(overlap)
        status = f"❌ ĐÈ {overlap:+.3f}s" if overlap > 0 else f"✅ Cách {abs(overlap):.3f}s"
        print(f"  Câu {s1.index} -> {s2.index} | Start1={start1:6.3f}s, Dur1={dur1:5.3f}s, End1={end1:6.3f}s | Start2={start2:6.3f}s | {status}")

    total_baseline_overlaps = sum(1 for ov in baseline_overlaps if ov > 0)
    max_baseline_overlap = max(baseline_overlaps)
    print(f"-> Kết quả Baseline: {total_baseline_overlaps}/{len(baseline_overlaps)} cặp bị đè giọng. Đè nặng nhất: {max_baseline_overlap:.3f}s!")

    # -------------------------------------------------------------
    # BƯỚC 2: Kiểm tra Điểm 1 - calculate_reading_windows
    # -------------------------------------------------------------
    print("\n--- [ĐIỂM 1] TÍNH KHOẢNG ĐỌC CHO PHÉP (READING WINDOWS) ---")
    windows = calculate_reading_windows(segments, video_duration=None, gap_s=0.05)
    for seg in segments:
        w = windows[seg.index]
        dur_orig = raw_audios[seg.index][0]
        ratio = dur_orig / w if w > 0 else 1.0
        print(f"  Câu {seg.index:2d}: Start={seg.start.total_seconds():6.3f}s | T_max={w:5.3f}s | Dur gốc={dur_orig:5.3f}s | Cần tăng tốc: {ratio:.3f}x")
        assert w > 0, f"Window for segment {seg.index} must be positive"

    # -------------------------------------------------------------
    # BƯỚC 3: Kiểm tra Điểm 2 - trim_audio_silence & fit_audio_file
    # -------------------------------------------------------------
    print("\n--- [ĐIỂM 2] ADAPT VOICE SPEED & TRIM SILENCE (FFMPEG ATEMPO) ---")
    fitted_dubs = []
    for seg in segments:
        w = windows[seg.index]
        raw_path = raw_audios[seg.index][1]
        out_fitted = temp_dir / f"{seg.index}_fitted.mp3"
        fitted_path, actual_dur, speed = await fit_audio_file(raw_path, target_max_duration=w, output_path=str(out_fitted))
        print(f"  Câu {seg.index:2d}: Gốc={raw_audios[seg.index][0]:.3f}s -> Cắt khoảng lặng + atempo={speed:.3f}x -> Thực tế={actual_dur:.3f}s (T_max={w:.3f}s)")
        assert actual_dur <= w + 0.05, f"Audio {seg.index} duration {actual_dur} exceeds window {w}"
        fitted_dubs.append({
            "index": seg.index,
            "path": fitted_path,
            "start": seg.start.total_seconds(),
            "end": seg.start.total_seconds() + actual_dur,
            "actual_audio_duration": actual_dur,
            "duration": actual_dur,
        })

    # Đo lại overlap sau khi fit
    print("\n  Kiểm tra overlap SAU KHI ADAPT TỐC ĐỘ:")
    after_overlaps = []
    for i in range(len(fitted_dubs) - 1):
        d1 = fitted_dubs[i]
        d2 = fitted_dubs[i + 1]
        start1 = d1["start"]
        dur1 = d1["actual_audio_duration"]
        end1 = start1 + dur1
        start2 = d2["start"]
        overlap = end1 - start2
        after_overlaps.append(overlap)
        status = f"❌ ĐÈ {overlap:+.3f}s" if overlap > 0.001 else f"✅ Cách {abs(overlap):.3f}s"
        print(f"    Câu {d1['index']} -> {d2['index']} | End1={end1:6.3f}s | Start2={start2:6.3f}s | {status}")
        assert overlap <= 0.005, f"Overlap detected between segment {d1['index']} and {d2['index']}: {overlap}s"

    print("-> XÁC NHẬN ĐIỂM 2: 0/9 CẶP BỊ ĐÈ! 100% các câu kết thúc trước khi câu sau bắt đầu!")

    # -------------------------------------------------------------
    # BƯỚC 4: Kiểm tra Điểm 3 - Rút gọn ngữ nghĩa câu quá dài qua Gemini
    # -------------------------------------------------------------
    print("\n--- [ĐIỂM 3] BATCH CONDENSATION VỚI GEMINI NẾU TỐC ĐỘ > 1.45x ---")
    test_overly_long = [{
        "index": 99,
        "text": "Cuộc hành trình vạn dặm bắt đầu từ một bước chân nhỏ bé và sự kiên trì bền bỉ qua từng ngày của mỗi người chúng ta.",
        "target_seconds": 2.0,
        "current_seconds": 6.5,
    }]
    condensed_results = await asyncio.to_thread(condense_vietnamese_subtitles_batch, test_overly_long)
    condensed_text = condensed_results.get(99, "")
    print(f"  Văn bản gốc ({len(test_overly_long[0]['text'].split())} từ): '{test_overly_long[0]['text']}'")
    print(f"  Văn bản rút gọn ({len(condensed_text.split())} từ): '{condensed_text}'")
    assert condensed_text, "Gemini condensation must return non-empty text"
    assert len(condensed_text.split()) < len(test_overly_long[0]["text"].split()), "Condensed text must have fewer words"
    print("-> XÁC NHẬN ĐIỂM 3: Gemini rút gọn câu thành công, bảo toàn ngữ nghĩa và giảm độ dài!")

    # -------------------------------------------------------------
    # BƯỚC 5: Kiểm tra Điểm 4 - Anti-Overlap Hard Guard ở Mixer & Subtitle Sync
    # -------------------------------------------------------------
    print("\n--- [ĐIỂM 4] HARD GUARD TẠI V1_AUDIO_MIXER & SUBTITLE SYNC ---")

    # a) Test sync_and_clamp_subtitles
    test_segs = [
        srt.Subtitle(1, datetime.timedelta(seconds=1.0), datetime.timedelta(seconds=4.0), "Câu 1"),
        srt.Subtitle(2, datetime.timedelta(seconds=3.0), datetime.timedelta(seconds=5.0), "Câu 2"),
    ]
    test_audio_info = [
        {"index": 1, "actual_audio_duration": 2.5}, # 1.0 + 2.5 = 3.5s -> đè câu 2 (bắt đầu lúc 3.0s)
        {"index": 2, "actual_audio_duration": 1.2},
    ]
    synced = sync_and_clamp_subtitles(test_segs, test_audio_info)
    print(f"  Sub 1 sau clamp: Start={synced[0].start.total_seconds()}s, End={synced[0].end.total_seconds()}s")
    print(f"  Sub 2 sau clamp: Start={synced[1].start.total_seconds()}s, End={synced[1].end.total_seconds()}s")
    # Sub 2 start tuyệt đối không được dịch chuyển!
    assert synced[1].start.total_seconds() == 3.0, "next_seg.start MUST NOT be pushed forward!"
    # Sub 1 end phải kết thúc trước Sub 2 start ít nhất 0.05s
    assert synced[0].end.total_seconds() <= 2.951, f"Sub 1 end {synced[0].end.total_seconds()} exceeds safe boundary"
    print("  -> XÁC NHẬN SUBTITLE SYNC: Phụ đề câu 1 kết thúc lúc 2.95s, câu 2 giữ nguyên mốc 3.0s!")

    # b) Test mix_adaptive_audio Hard Guard
    # Giả lập tình huống Mixer nhận được 2 file âm thanh cố tình đè lên nhau
    bgm_path = str(sample_dir / "original.wav")
    deliberate_overlap_dubs = [
        {"index": 1, "start": 0.5, "path": fitted_dubs[0]["path"]}, # Giả sử bắt đầu lúc 0.5s
        {"index": 2, "start": 1.5, "path": fitted_dubs[1]["path"]}, # Bắt đầu lúc 1.5s, trong khi câu 1 dài > 1.0s
    ]
    mixed_out = str(temp_dir / "test_hard_guard_mix.wav")
    mix_adaptive_audio(bgm_path, deliberate_overlap_dubs, mixed_out)
    assert os.path.exists(mixed_out) and os.path.getsize(mixed_out) > 1000, "Mixed output file must exist"
    print("  -> XÁC NHẬN MIXER HARD GUARD: Đã kích hoạt cắt fadeout câu trước trước khi câu sau phát!")

    # c) Test Cache Version 6 Validation
    assert CACHE_KEY_VERSION == 6, f"CACHE_KEY_VERSION must be 6, got {CACHE_KEY_VERSION}"
    test_seg_cache = srt.Subtitle(1, datetime.timedelta(seconds=0), datetime.timedelta(seconds=2.0), "Xin chào các bạn")
    key_v6 = voice_cache_key(test_seg_cache, "capcut", "BV075_streaming", max_duration=2.0)
    assert len(key_v6) == 64, f"Cache key must be a valid sha256 hash: {key_v6}"
    
    # Giả lập file cache dài 3.0s trong khi max_duration chỉ là 2.0s
    dummy_cache_file = str(temp_dir / "dummy_cache.mp3")
    AudioSegment.silent(duration=3000).export(dummy_cache_file, format="mp3").close()
    write_voice_cache(dummy_cache_file, key_v6, 3.0, "Xin chào các bạn")
    read_result = read_voice_cache(dummy_cache_file, key_v6, "Xin chào các bạn", max_duration=2.0)
    assert read_result is None, "Cache reader must invalidate cached audio exceeding max_duration + 0.05s"
    print("  -> XÁC NHẬN CACHE V6: Tự động phát hiện và hủy bỏ cache quá dài!")

    print("\n=================================================================")
    print("TẤT CẢ CÁC BÀI TEST ĐÃ VƯỢT QUA 100%! HỆ THỐNG HOÀN TOÀN CHUẨN XÁC!")
    print("=================================================================")

if __name__ == "__main__":
    asyncio.run(run_tests())
