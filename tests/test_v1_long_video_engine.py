"""
test_v1_long_video_engine.py - Bộ kiểm thử tự động cho Long Video Engine (Giai đoạn 4).
Kiểm tra:
1. ASR Chunking (240s với 0.75s overlap) & Khử trùng lặp tại ranh giới chunk.
2. Timing Solver: Căn chỉnh timeline, co giãn nhịp điệu [0.85x - 1.35x], gom batch nén Gemini.
3. Hierarchical Audio Mixer: Phân cụm 300s, tránh Win32 command line limit, concat demuxer.
"""

import os
import sys
import unittest
import tempfile
from pathlib import Path
from unittest import mock

# Ensure import paths work under pytest
try:
    from backend.ai.v1_asr_chunking import (
        chunk_audio_intervals,
        deduplicate_boundary_segments,
        DEFAULT_CHUNK_SIZE_S,
        DEFAULT_OVERLAP_S,
    )
    from backend.v1_timing_solver import (
        calculate_timing_windows,
        calculate_atempo_factor,
        detect_overly_long_segments,
        build_safe_atempo_filter,
        plan_condensation_batches,
        DEFAULT_MAX_NATURAL_SPEED,
    )
    from backend.v1_hierarchical_mixer import (
        partition_audio_clusters,
        DEFAULT_CLUSTER_SIZE_S,
    )
except ImportError:
    from ai.v1_asr_chunking import (
        chunk_audio_intervals,
        deduplicate_boundary_segments,
        DEFAULT_CHUNK_SIZE_S,
        DEFAULT_OVERLAP_S,
    )
    from v1_timing_solver import (
        calculate_timing_windows,
        calculate_atempo_factor,
        detect_overly_long_segments,
        build_safe_atempo_filter,
        plan_condensation_batches,
        DEFAULT_MAX_NATURAL_SPEED,
    )
    from v1_hierarchical_mixer import (
        partition_audio_clusters,
        DEFAULT_CLUSTER_SIZE_S,
    )


class DummySubtitle:
    def __init__(self, index, start, end, content):
        self.index = index
        self.start = start
        self.end = end
        self.content = content


class LongVideoEngineTests(unittest.TestCase):

    # ===== 1. ASR CHUNKING TESTS =====

    def test_asr_chunk_intervals_short_video(self):
        """Video <= 240s không bị chia cắt, giữ nguyên 1 chunk duy nhất."""
        intervals = chunk_audio_intervals(180.0, chunk_size=240.0, overlap=0.75)
        self.assertEqual(len(intervals), 1)
        self.assertEqual(intervals[0], (0.0, 180.0))

    def test_asr_chunk_intervals_long_video(self):
        """Video dài 600s (10 phút) được chia thành các khoảng 240s với 0.75s overlap."""
        intervals = chunk_audio_intervals(600.0, chunk_size=240.0, overlap=0.75)
        self.assertGreater(len(intervals), 1)
        # Chunk đầu: [0, 240]
        self.assertEqual(intervals[0], (0.0, 240.0))
        # Chunk thứ 2 phải bắt đầu từ 240.0 - 0.75 = 239.25
        self.assertAlmostEqual(intervals[1][0], 239.25, places=2)
        # Chunk cuối cùng phải chạm mốc 600.0
        self.assertEqual(intervals[-1][1], 600.0)

    def test_asr_deduplicate_boundary_segments(self):
        """Khử trùng lặp câu/từ xuất hiện 2 lần ở ranh giới 2 chunk."""
        prev_chunk = [
            {"start": 235.0, "end": 239.5, "text": "Đây là câu kết thúc của chunk một."},
        ]
        next_chunk = [
            # Câu lặp lại do overlap
            {"start": 239.3, "end": 239.6, "text": "Đây là câu kết thúc của chunk một."},
            # Câu mới thực sự
            {"start": 240.5, "end": 243.0, "text": "Đây là câu mới bắt đầu của chunk hai."},
        ]
        deduped = deduplicate_boundary_segments(prev_chunk, next_chunk, overlap_point=239.25)
        self.assertEqual(len(deduped), 1)
        self.assertEqual(deduped[0]["text"], "Đây là câu mới bắt đầu của chunk hai.")

    # ===== 2. TIMING SOLVER TESTS =====

    def test_timing_windows_calculation(self):
        """Tính toán khoảng đệm an toàn giữa các câu thoại (chống audio overlap)."""
        subs = [
            DummySubtitle(index=1, start=0.0, end=2.0, content="Câu một"),
            DummySubtitle(index=2, start=3.0, end=5.0, content="Câu hai"),
            DummySubtitle(index=3, start=6.0, end=8.0, content="Câu ba"),
        ]
        windows = calculate_timing_windows(subs, total_audio_duration=10.0, hard_gap_s=0.08)
        self.assertEqual(len(windows), 3)
        # Cửa sổ câu 1: next_start(3.0) - cur_start(0.0) - gap(0.08) = 2.92s
        self.assertAlmostEqual(windows[1], 2.92, places=2)
        # Cửa sổ câu 2: next_start(6.0) - cur_start(3.0) - gap(0.08) = 2.92s
        self.assertAlmostEqual(windows[2], 2.92, places=2)

    def test_atempo_factor_boundaries(self):
        """Kiểm tra co giãn tốc độ an toàn: bình thường, fit atempo, hoặc kích hoạt nén Gemini."""
        # 1. Bình thường (không tràn khung hoặc ratio <= 1.03)
        factor, status = calculate_atempo_factor(actual_duration=2.0, window_duration=2.5)
        self.assertEqual(factor, 1.0)
        self.assertEqual(status, "normal")

        # 2. Tràn nhẹ trong ngưỡng [1.03, 1.35] -> atempo_fit
        factor, status = calculate_atempo_factor(actual_duration=2.4, window_duration=2.0)
        self.assertAlmostEqual(factor, 1.2, places=1)
        self.assertEqual(status, "atempo_fit")

        # 3. Tràn quá mức (> 1.35x) -> needs_condensation
        factor, status = calculate_atempo_factor(actual_duration=3.5, window_duration=2.0)
        self.assertAlmostEqual(factor, 1.35, places=2)
        self.assertEqual(status, "needs_condensation")

    def test_detect_overly_long_and_batch_planning(self):
        """Phát hiện các câu quá dài và gom thành từng lô nén gọn gàng."""
        subs = [
            DummySubtitle(index=1, start=0.0, end=1.5, content="Một câu rất dài có quá nhiều từ ngữ giải thích chi tiết kỹ thuật không thể đọc kịp trong một giây"),
            DummySubtitle(index=2, start=2.0, end=4.0, content="Câu ngắn vừa vặn"),
        ]
        windows = {1: 1.0, 2: 2.5}
        overly_long = detect_overly_long_segments(subs, windows, max_natural_speed=1.35)
        self.assertEqual(len(overly_long), 1)
        self.assertEqual(overly_long[0]["index"], 1)

        batches = plan_condensation_batches(overly_long, batch_size=20)
        self.assertEqual(len(batches), 1)
        self.assertEqual(len(batches[0]), 1)

    def test_build_safe_atempo_filter(self):
        """Tạo chuỗi filter atempo hợp lệ của FFmpeg."""
        f_normal = build_safe_atempo_filter(1.0)
        self.assertEqual(f_normal, "anull")

        f_fast = build_safe_atempo_filter(1.25)
        self.assertEqual(f_fast, "atempo=1.250")

        f_ultra = build_safe_atempo_filter(2.5)
        self.assertIn("atempo=2.0", f_ultra)

    # ===== 3. HIERARCHICAL AUDIO MIXER TESTS =====

    def test_partition_audio_clusters(self):
        """Phân cụm 300s (5 phút) cho video dài."""
        dubs = [
            {"index": 1, "start": 10.0, "path": "d1.wav"},
            {"index": 2, "start": 290.0, "path": "d2.wav"},
            {"index": 3, "start": 310.0, "path": "d3.wav"},
            {"index": 4, "start": 650.0, "path": "d4.wav"},
        ]
        clusters = partition_audio_clusters(total_duration_s=700.0, dubbing_audio_files=dubs, cluster_size_s=300.0)
        # 700s / 300s = 3 clusters ([0-300], [300-600], [600-700])
        self.assertEqual(len(clusters), 3)
        self.assertEqual(len(clusters[0]["dubs"]), 2)  # d1 (10s), d2 (290s)
        self.assertEqual(len(clusters[1]["dubs"]), 1)  # d3 (310s)
        self.assertEqual(len(clusters[2]["dubs"]), 1)  # d4 (650s)


if __name__ == "__main__":
    unittest.main()
