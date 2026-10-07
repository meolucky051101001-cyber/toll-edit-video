"""
test_v1_quality_gate.py - Bộ kiểm thử tự động cho Quality Gate 8 nhóm (Tool V1 - Giai đoạn 5).
Kiểm tra:
1. Xuất báo cáo QC đầy đủ 8 nhóm kiểm tra tự động.
2. Phát hiện lỗi CJK sót, câu rỗng, đè câu, clipping âm lượng.
3. Hoạt động của các chính sách REPORT_ONLY, WARN, BLOCK.
"""

import os
import sys
import unittest
import tempfile
import json
from pathlib import Path

try:
    from backend.v1_quality_gate import (
        run_quality_gate,
        QCPolicy,
        QCCategory,
        QCReport,
    )
except ImportError:
    from v1_quality_gate import (
        run_quality_gate,
        QCPolicy,
        QCCategory,
        QCReport,
    )


class DummySub:
    def __init__(self, index, content):
        self.index = index
        self.content = content


class QualityGateTests(unittest.TestCase):

    def test_invalid_ass_and_missing_tts_are_not_passed(self):
        """A mocked first frame does not prove valid subtitles or complete speech."""
        from unittest.mock import patch, MagicMock
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp = Path(tmp_dir)
            video = tmp / "output.mp4"
            video.write_bytes(b"dummy video data" * 1000)

            ass = tmp / "output.ass"
            ass.write_text("[Script Info]\nTitle: Test\n", encoding="utf-8")

            subs = [
                DummySub(1, "Xin chào các bạn"),
                DummySub(2, "Hôm nay chúng ta cùng tìm hiểu"),
            ]

            with patch("cv2.VideoCapture") as mock_cap_cls:
                mock_cap = MagicMock()
                mock_cap.isOpened.return_value = True
                mock_cap.get.side_effect = lambda prop: 120 if prop == 7 else (30.0 if prop == 5 else (1920 if prop == 3 else (1080 if prop == 4 else 0)))
                mock_cap.read.return_value = (True, "valid_frame")
                mock_cap_cls.return_value = mock_cap

                with self.assertRaises(RuntimeError):
                    run_quality_gate(
                        job_id="test_qc_01", final_video_path=video,
                        translated_subtitles=subs, ass_subtitle_path=ass,
                        policy="REPORT_ONLY", workspace_path=tmp,
                    )

            # File JSON được lưu trên đĩa
            json_file = tmp / "bot_system" / "qc_reports" / "test_qc_01.qc.json"
            self.assertTrue(json_file.is_file())
            report = json.loads(json_file.read_text(encoding="utf-8"))
            self.assertEqual(report["overall_status"], "FAILED")
            self.assertTrue(any(c["name"] == "tts_cue_coverage" and c["status"] == "FAIL" for c in report["checks"]))

    def test_qc_detect_cjk_and_empty_translation(self):
        """Phát hiện cảnh báo khi bản dịch sót chữ Hán hoặc rỗng."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp = Path(tmp_dir)
            video = tmp / "output.mp4"
            video.write_bytes(b"dummy video" * 1000)

            subs = [
                DummySub(1, "Đây là câu có chữ Hán 显卡"),
                DummySub(2, "   "),  # Rỗng
                DummySub(3, "Câu hợp lệ"),
            ]

            with self.assertRaises(RuntimeError):
                run_quality_gate(job_id="test_qc_cjk", final_video_path=video,
                    translated_subtitles=subs, policy="REPORT_ONLY", workspace_path=tmp)
            report = json.loads((tmp / "bot_system/qc_reports/test_qc_cjk.qc.json").read_text(encoding="utf-8"))
            cjk_check = next((c for c in report["checks"] if c["name"] == "no_cjk_residual"), None)
            self.assertIsNotNone(cjk_check)
            self.assertEqual(cjk_check["status"], "FAIL")

            empty_check = next((c for c in report["checks"] if c["name"] == "no_empty_subtitles"), None)
            self.assertIsNotNone(empty_check)
            self.assertEqual(empty_check["status"], "FAIL")

    def test_report_only_blocks_missing_output_but_keeps_report(self):
        """REPORT_ONLY is not permission to publish a missing output file."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp = Path(tmp_dir)
            fake_video = tmp / "non_existent.mp4"

            with self.assertRaises(RuntimeError):
                run_quality_gate(job_id="test_qc_fail", final_video_path=fake_video,
                    policy="REPORT_ONLY", workspace_path=tmp)
            report = json.loads((tmp / "bot_system/qc_reports/test_qc_fail.qc.json").read_text(encoding="utf-8"))
            self.assertEqual(report["overall_status"], "FAILED")
            self.assertGreater(report["failure_count"], 0)

    def test_qc_block_policy_raises_on_failure(self):
        """Chế độ BLOCK sẽ ném RuntimeError khi nghiệm thu thất bại."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp = Path(tmp_dir)
            fake_video = tmp / "non_existent.mp4"

            with self.assertRaises(RuntimeError):
                run_quality_gate(
                    job_id="test_qc_block",
                    final_video_path=fake_video,
                    policy="BLOCK",
                    workspace_path=tmp,
                )


if __name__ == "__main__":
    unittest.main()
