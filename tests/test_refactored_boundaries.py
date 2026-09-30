import tempfile
import unittest
from datetime import timedelta
from pathlib import Path

from backend.pipeline_v2.content import compose_srt, merge_ocr_geometry
from backend.pipeline_v2.segments import RuntimeSegment
from backend.telegram_jobs import (
    TelegramJobPaths,
    build_v2_completion_caption,
    format_elapsed_time,
)


class RefactoredBoundaryTests(unittest.TestCase):
    def test_telegram_job_paths_keep_existing_names_and_output_contract(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            paths = TelegramJobPaths.create(
                str(root / "workspace"), str(root / "banve"), "video-1"
            )
            paths.prepare_directories()

            self.assertEqual(paths.final_video.name, "final_video-1.mp4")
            self.assertEqual(paths.delivery_copy.name, "Dubbed_video-1.mp4")
            self.assertTrue(paths.job_directory.is_dir())
            self.assertTrue(paths.delivery_copy.parent.is_dir())

    def test_completion_caption_preserves_duration_and_queue_status(self):
        caption = build_v2_completion_caption("clip.mp4", "D:/banve", 125, 2)
        self.assertIn("clip.mp4", caption)
        self.assertIn("2 phút 5 giây", caption)
        self.assertIn("2 video", caption)
        self.assertEqual(format_elapsed_time(-1), "0 giây")

    def test_content_helpers_preserve_srt_and_ocr_geometry(self):
        source = RuntimeSegment(
            index=1,
            start=timedelta(seconds=1),
            end=timedelta(seconds=2.25),
            content="Xin chào",
        )
        geometry = RuntimeSegment(
            index=1,
            start=source.start,
            end=source.end,
            content="原文",
            y_pct=0.8,
            max_y_pct=0.86,
        )

        self.assertIn("00:00:01,000 --> 00:00:02,250", compose_srt([source]))
        merged = merge_ocr_geometry([source], [geometry])
        self.assertEqual(merged[0].content, "Xin chào")
        self.assertEqual(merged[0].y_pct, 0.8)
        self.assertEqual(merged[0].max_y_pct, 0.86)


if __name__ == "__main__":
    unittest.main()
