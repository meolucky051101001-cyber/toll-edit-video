import ast
import unittest
from pathlib import Path


ROOT = Path(__file__).parents[1]


class LegacyPatchPreservationTests(unittest.TestCase):
    def test_nvenc_and_unique_subtitle_cleanup_patch_remain(self):
        source = (ROOT / "backend" / "video_utils.py").read_text(encoding="utf-8")
        for token in (
            "temp_burn_",
            "uuid.uuid4().hex",
            "finally:",
            "h264_nvenc",
            "'-preset', 'p4'",
            "'-tune', 'hq'",
            "'-spatial-aq', '1'",
        ):
            self.assertIn(token, source)

    def test_telegram_network_updates_use_safe_wrapper(self):
        source = (ROOT / "backend" / "telegram_bot.py").read_text(encoding="utf-8")
        module = ast.parse(source)
        function = next(
            node
            for node in module.body
            if isinstance(node, ast.AsyncFunctionDef)
            and node.name == "safe_edit_status"
        )
        body = ast.get_source_segment(source, function)
        self.assertIn("for attempt in range(retries)", body)
        self.assertIn("except Exception", body)
        self.assertIn("await asyncio.sleep", body)
        self.assertEqual(source.count(".edit_text("), 1)

    def test_placeholder_secrets_are_not_treated_as_credentials(self):
        source = (ROOT / "backend" / "telegram_bot.py").read_text(encoding="utf-8")
        self.assertIn("def configured_secret", source)
        self.assertIn('value.upper().startswith(("YOUR_", "PASTE_"))', source)
        self.assertIn("if not BOT_TOKEN:", source)

    def test_gemini_keeps_inline_video_frame_context(self):
        source = (ROOT / "backend" / "ai" / "translation.py").read_text(
            encoding="utf-8"
        )
        self.assertIn("sample_video_frames(video_path, num_frames", source)
        self.assertIn('"inline_data"', source)
        self.assertIn('"mime_type": "image/jpeg"', source)
        import base64
        import numpy as np
        import tempfile
        from unittest.mock import patch
        from backend.ai.translation import extract_video_frames_base64
        import cv2
        frames = [np.full((32, 48, 3), i * 40, dtype=np.uint8) for i in range(5)]
        with tempfile.TemporaryDirectory() as directory:
            video = Path(directory) / 'fixture.mp4'
            video.write_bytes(b'fixture')
            with patch('backend.video_sampling.sample_video_frames', return_value=frames) as sample:
                encoded = extract_video_frames_base64(str(video), 2, 8, 5)
            sample.assert_called_once_with(str(video), 5, 2, 8)
        self.assertEqual(len(encoded), 5)
        for jpeg in encoded:
            decoded = cv2.imdecode(np.frombuffer(base64.b64decode(jpeg), np.uint8), cv2.IMREAD_COLOR)
            self.assertEqual(decoded.shape, (32, 48, 3))

    def test_v2_gpu_limits_are_explicit(self):
        pipeline = (ROOT / "backend" / "pipeline_v2" / "video_pipeline.py").read_text(
            encoding="utf-8"
        )
        worker = (ROOT / "backend" / "pipeline_v2" / "gpu_worker.py").read_text(
            encoding="utf-8"
        )
        self.assertIn('"segment_seconds": 6', pipeline)
        self.assertIn('"num_workers": 1', pipeline)
        self.assertIn('payload.get("segment_seconds", 6.0)', worker)
        self.assertIn('payload.get("num_workers", 1)', worker)

    def test_fastapi_processing_routes_share_v2_runner(self):
        source = (ROOT / "backend" / "main.py").read_text(encoding="utf-8")
        self.assertIn("async def run_api_pipeline_v2", source)
        self.assertEqual(source.count("await run_api_pipeline_v2("), 2)
        self.assertIn('"pipeline": "v2"', source)
        self.assertEqual(source.count("await API_PROCESS_LOCK.acquire()"), 2)
        self.assertEqual(source.count("API_PROCESS_LOCK.release()"), 2)
        self.assertIn("time.time_ns()", source)
        self.assertIn(
            'tts_api_key=(api_key if voice_source == "fpt" else "")', source
        )
        self.assertNotIn('allow_origins=["*"]', source)
        self.assertIn("AUTODUB_CORS_ORIGINS", source)

    def test_local_renderer_uses_portable_environment_paths(self):
        source = (ROOT / "backend" / "render_video_phoi.py").read_text(
            encoding="utf-8-sig"
        )
        self.assertIn('os.getenv("AUTODUB_WORKSPACE"', source)
        self.assertIn('os.getenv("AUTODUB_INPUT_DIR"', source)
        self.assertIn('os.getenv("AUTODUB_OUTPUT_DIR"', source)
        self.assertNotIn(r"C:\Users\admin\.gemini", source)


if __name__ == "__main__":
    unittest.main()
