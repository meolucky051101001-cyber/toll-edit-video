"""Regression coverage for the fixed CapCut voice despite old gender metadata."""

import sys
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest import mock

from backend.pipeline_v2.segments import RuntimeSegment
from backend.pipeline_v2.tts import generate_tts_audio_v2


def segment(index, gender):
    return RuntimeSegment(index=index, start=timedelta(seconds=index - 1),
                          end=timedelta(seconds=index), content="Xin chào", gender=gender)


def fake_fit(source, target, duration, policy):
    Path(target).write_bytes(Path(source).read_bytes())
    return SimpleNamespace(source_duration_seconds=duration, target_duration_seconds=duration,
                           output_duration_seconds=duration, applied_atempo=1.0, fits=True)


class SingleVoiceTests(unittest.IsolatedAsyncioTestCase):
    async def test_tts_ignores_male_metadata_unless_enabled(self):
        voices = []
        module = ModuleType("ai.voice_cloning")
        module.FPTQuotaError = RuntimeError
        module.generate_tts_edge = mock.AsyncMock(side_effect=AssertionError("unexpected Edge"))
        module.generate_tts_fpt = mock.AsyncMock(side_effect=AssertionError("unexpected FPT"))

        def capcut(text, output, voice):
            voices.append(voice)
            Path(output).write_bytes(b"tts")

        module._run_capcut_tts = capcut
        with tempfile.TemporaryDirectory() as directory, mock.patch.dict(
            sys.modules, {"ai.voice_cloning": module}
        ), mock.patch("backend.pipeline_v2.tts.fit_audio_to_window", side_effect=fake_fit):
            await generate_tts_audio_v2([segment(1, "male"), segment(2, "female")],
                                        directory, voice_source="capcut",
                                        voice_param="BV562_streaming")
            self.assertEqual(voices, ["BV562_streaming", "BV562_streaming"])
            voices.clear()
            await generate_tts_audio_v2([segment(1, "male"), segment(2, "female")], directory,
                                        voice_source="capcut", voice_param="BV562_streaming",
                                        enable_auto_gender=True)
            self.assertEqual(voices, ["BV075_streaming", "BV562_streaming"])

    async def test_retired_rvc_provider_fails_closed(self):
        module = ModuleType("ai.voice_cloning")
        module.FPTQuotaError = RuntimeError
        module.generate_tts_edge = mock.AsyncMock()
        module.generate_tts_fpt = mock.AsyncMock()
        module._run_capcut_tts = mock.Mock()
        with tempfile.TemporaryDirectory() as directory, mock.patch.dict(
            sys.modules, {"ai.voice_cloning": module}
        ), mock.patch("backend.pipeline_v2.tts.fit_audio_to_window", side_effect=fake_fit):
            with self.assertRaisesRegex(ValueError, "Unsupported TTS provider"):
                await generate_tts_audio_v2([segment(1, "male")], directory,
                                            voice_source="rvc")


if __name__ == "__main__":
    unittest.main()
