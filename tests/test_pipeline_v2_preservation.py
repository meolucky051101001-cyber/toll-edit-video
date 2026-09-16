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
        self.assertIn("cv2.VideoCapture(video_path)", source)
        self.assertIn('"inline_data"', source)
        self.assertIn('"mime_type": "image/jpeg"', source)

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
