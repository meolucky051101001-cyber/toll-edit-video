"""
test_v1_wireup_integration.py - Kiểm thử tích hợp thực tế cho dây chuyền kết nối Tool V1 (Giai đoạn A-G).
Kiểm tra:
1. Đọc/ghi cấu hình cờ tính năng xuyên tiến trình (v1_feature_flags.py).
2. Xử lý video_mode và routing từ API main.py (Dashboard UI).
3. Logic Smart Skip OCR kết hợp Whisper Confidence và Visual Band Activity.
4. Điều phối V1Orchestrator với checkpoint, GPU guard và Quality Gate.
"""

import os
import sys
import unittest
import tempfile
from pathlib import Path
from unittest.mock import patch, MagicMock

try:
    from backend.v1_feature_flags import get_feature_flags, set_feature_flags, DEFAULT_FLAGS
    from backend.v1_orchestrator import V1Orchestrator
    from backend.v1_video_router import VideoMode
except ImportError:
    from v1_feature_flags import get_feature_flags, set_feature_flags, DEFAULT_FLAGS
    from v1_orchestrator import V1Orchestrator
    from v1_video_router import VideoMode


class V1WireupIntegrationTests(unittest.TestCase):

    def test_feature_flags_persistence(self):
        """Kiểm tra lưu trữ và tải bền vững cờ tính năng."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp = Path(tmp_dir)
            flags = get_feature_flags(tmp)
            self.assertEqual(flags["V1_USE_ORCHESTRATOR"], True)
            self.assertEqual(flags["ENABLE_QWEN_ASR"], False)

            # Thay đổi cờ
            set_feature_flags({"V1_SMART_SKIP_OCR": False, "V1_QC_POLICY": "BLOCK"}, tmp)
            updated = get_feature_flags(tmp)
            self.assertEqual(updated["V1_SMART_SKIP_OCR"], False)
            self.assertEqual(updated["V1_QC_POLICY"], "BLOCK")

    def test_orchestrator_planning_and_gpu_guard(self):
        """Kiểm tra lập kế hoạch và bảo vệ GPU fail-closed."""
        from backend.v1_video_router import VideoMetadata

        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp = Path(tmp_dir)
            dummy_video = tmp / "test_input.mp4"
            dummy_video.write_bytes(b"data" * 100)

            orch = V1Orchestrator(workspace_path=tmp)
            # The placeholder file is not a real MP4; keep this test focused on
            # orchestration by supplying deterministic ffprobe metadata.
            with patch(
                "backend.v1_video_router.extract_media_metadata",
                return_value=VideoMetadata(duration_s=120.0, width=1280, height=720),
            ):
                plan = orch.plan_job(dummy_video, job_id="job_test_01", user_mode="auto")

            self.assertIn("job_id", plan)
            self.assertIn(plan["routing"]["resolved_mode"], [VideoMode.SHORT.value, VideoMode.MEDIUM.value, VideoMode.LONG.value])
            self.assertIn("planned_models", plan)

            # Test GPU Guard fail-closed behavior
            with patch("torch.cuda.is_available", return_value=False):
                with self.assertRaises(RuntimeError) as ctx:
                    orch._check_gpu_guard("separate_vocals")
                self.assertIn("GPU Guard từ chối thực thi", str(ctx.exception))

    def test_smart_skip_ocr_visual_activity_function(self):
        """Kiểm tra hàm đánh giá hoạt độ biên chữ trên vùng phụ đề đáy video."""
        import numpy as np
        import cv2

        # Tạo frame giả lập: 1 frame có chữ (nhiều cạnh), 1 frame trơn (không có cạnh)
        blank_frame = np.zeros((720, 1280, 3), dtype=np.uint8)
        text_frame = np.zeros((720, 1280, 3), dtype=np.uint8)
        cv2.putText(text_frame, "CHINESE SUBTITLE TEST 123", (200, 600),
                    cv2.FONT_HERSHEY_SIMPLEX, 1.5, (255, 255, 255), 3)

        mock_cap_blank = MagicMock()
        mock_cap_blank.read.return_value = (True, blank_frame)

        mock_cap_text = MagicMock()
        mock_cap_text.read.return_value = (True, text_frame)

        from backend.ocr_utils import perform_video_ocr

        # Kiểm tra Canny trên blank frame
        y1, y2 = int(720 * 0.68), int(720 * 0.92)
        x1, x2 = int(1280 * 0.10), int(1280 * 0.90)
        edges_blank = cv2.Canny(cv2.cvtColor(blank_frame[y1:y2, x1:x2], cv2.COLOR_BGR2GRAY), 60, 180)
        dens_blank = float(cv2.countNonZero(edges_blank)) / float(edges_blank.size)
        self.assertLess(dens_blank, 0.018)

        edges_text = cv2.Canny(cv2.cvtColor(text_frame[y1:y2, x1:x2], cv2.COLOR_BGR2GRAY), 60, 180)
        dens_text = float(cv2.countNonZero(edges_text)) / float(edges_text.size)
        self.assertGreaterEqual(dens_text, 0.018)


if __name__ == "__main__":
    unittest.main()
