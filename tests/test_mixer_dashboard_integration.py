"""Unit tests verifying dashboard audio volume settings integration with FFmpeg mixer V2."""

import asyncio
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch, MagicMock

from backend.pipeline_v2.video_pipeline import VideoPipelineRequest, VideoPipelineRunner
from backend.pipeline_v2.config import PipelineSettings


class TestMixerDashboardIntegration(unittest.IsolatedAsyncioTestCase):
    async def test_mix_v2_stage_uses_dashboard_audio_settings(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            workspace = root / "workspace"
            workspace.mkdir(parents=True, exist_ok=True)

            settings_file = workspace / "audio_settings.json"
            settings_file.write_text(json.dumps({
                "bgm_volume_db": -6.5,
                "dubbing_volume_db": 3.0,
            }), encoding="utf-8")

            request = VideoPipelineRequest(
                video_path=root / "input.mp4",
                job_directory=root / "job",
                output_path=root / "output.mp4",
                settings=PipelineSettings(),
            )
            runner = VideoPipelineRunner(request)
            runner._background_audio = MagicMock(return_value=root / "bgm.wav")
            runner._audio_infos = MagicMock(return_value=[])

            captured_settings = []
            def fake_mix(bg, infos, output, ffmpeg_settings, timeout_seconds=None):
                captured_settings.append(ffmpeg_settings)
                Path(output).write_bytes(b"mixed audio")

            with patch("backend.audio_settings.SETTINGS_FILE", settings_file), \
                 patch("backend.pipeline_v2.video_pipeline.mix_audio_ffmpeg", side_effect=fake_mix):
                await runner._mix_v2_stage()

            self.assertEqual(len(captured_settings), 1)
            cfg = captured_settings[0]
            self.assertEqual(cfg.background_gain_db, -6.5)
            self.assertEqual(cfg.voice_gain_db, 3.0)


if __name__ == "__main__":
    unittest.main()
