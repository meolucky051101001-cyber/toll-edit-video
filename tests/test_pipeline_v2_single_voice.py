"""Regression coverage for fixed Chí Mai voice despite old gender metadata."""

import sys
import tempfile
import unittest
from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest import mock

from backend.pipeline_v2.config import PipelineSettings
from backend.pipeline_v2.segments import RuntimeSegment
from backend.pipeline_v2.tts import generate_tts_audio_v2
from backend.pipeline_v2.video_pipeline import VideoPipelineRequest, VideoPipelineRunner


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
                                        directory, voice_source="rvc")
            self.assertEqual(voices, ["BV562_streaming", "BV562_streaming"])
            voices.clear()
            await generate_tts_audio_v2([segment(1, "male")], directory,
                                        voice_source="rvc", enable_auto_gender=True)
            self.assertEqual(voices, ["BV075_streaming"])

    async def test_rvc_fixed_voice_converts_all_segments_and_invalidates_mixed_cache(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            runner = VideoPipelineRunner(VideoPipelineRequest(
                video_path=root / "source.mp4", job_directory=root / "job",
                output_path=root / "output.mp4",
                settings=PipelineSettings(enable_auto_gender=True),
            ))
            runner.request = replace(runner.request, rvc_model_path=root / "ChiMai.pth")
            runner.manifest = SimpleNamespace(fingerprints=SimpleNamespace(model_sha256={"rvc": "test"}))
            infos = []
            for index, gender in ((1, "male"), (2, "female")):
                key = "tts/{}.mp3".format(index)
                runner.artifact_store.put_bytes(key, b"tts")
                infos.append({"index": index, "gender": gender, "artifact_key": key})
            runner.artifact_store.put_json("tts/segments.json", {"segments": infos})
            converted = []

            def gpu_run(stage, payload, timeout):
                self.assertEqual(stage, "rvc")
                self.assertEqual(payload["model_path"], str(root / "ChiMai.pth"))
                for item in payload["items"]:
                    converted.append(item["index"])
                    Path(item["output_path"]).write_bytes(b"Chimai")
                return {"items": payload["items"]}

            with mock.patch.object(runner.gpu_executor, "run", side_effect=gpu_run), mock.patch.object(
                runner, "_resource_scaled_timeout", return_value=30
            ), mock.patch("backend.pipeline_v2.video_pipeline.fit_audio_to_window", side_effect=fake_fit):
                segments = [segment(1, "male"), segment(2, "female")]
                await runner._rvc_stage(segments)
                self.assertEqual(converted, [2])
                converted.clear()
                runner.request = replace(runner.request, settings=replace(
                    runner.request.settings, enable_auto_gender=False))
                await runner._rvc_stage(segments)
                self.assertEqual(converted, [1, 2])
                for index in (1, 2):
                    self.assertEqual(runner.artifact_store.path_for("rvc/{}.wav".format(index)).read_bytes(), b"Chimai")


if __name__ == "__main__":
    unittest.main()
