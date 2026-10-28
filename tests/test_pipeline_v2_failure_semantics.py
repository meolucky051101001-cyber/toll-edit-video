import importlib
import sys
import tempfile
import unittest
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest import mock

from backend.pipeline_v2 import gpu_worker
from backend.pipeline_v2.config import PipelineMode, PipelineSettings
from backend.pipeline_v2.resume import ResumableVideoJob, resume_video_job
from backend.pipeline_v2.segments import RuntimeSegment
from backend.pipeline_v2.stage_validation import validate_demucs_outputs


class MediaStageValidationTests(unittest.TestCase):
    def test_demucs_rejects_legacy_source_audio_fallback(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "source.wav"
            source.write_bytes(b"source-audio")
            with self.assertRaisesRegex(RuntimeError, "source audio as a stem"):
                validate_demucs_outputs(source, source, source)

    def test_gpu_worker_rejects_demucs_fallback_even_when_files_exist(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "source.wav"
            source.write_bytes(b"source-audio")
            fake_video_utils = ModuleType("video_utils")
            fake_video_utils.separate_vocals_demucs = lambda *args, **kwargs: (
                str(source),
                str(source),
            )
            with mock.patch.dict(sys.modules, {"video_utils": fake_video_utils}):
                with self.assertRaisesRegex(RuntimeError, "source audio as a stem"):
                    gpu_worker._run_demucs(
                        {
                            "input_audio": str(source),
                            "output_directory": directory,
                        }
                    )

class TranslationFailureTests(unittest.TestCase):
    def _load_translation_with_failing_google(self):
        requests_module = ModuleType("requests")
        deep_module = ModuleType("deep_translator")

        class FailingGoogleTranslator:
            def __init__(self, *args, **kwargs):
                pass

            def translate(self, text):
                raise RuntimeError("offline")

        class FailingMyMemoryTranslator:
            def __init__(self, *args, **kwargs):
                pass

            def translate(self, text):
                raise RuntimeError("offline")

        deep_module.GoogleTranslator = FailingGoogleTranslator
        deep_module.MyMemoryTranslator = FailingMyMemoryTranslator
        sys.modules.pop("backend.ai.translation", None)
        with mock.patch.dict(
            sys.modules,
            {"requests": requests_module, "deep_translator": deep_module},
        ):
            return importlib.import_module("backend.ai.translation")

    def test_v2_strict_translation_rejects_source_text_fallback(self):
        translation = self._load_translation_with_failing_google()
        segment = SimpleNamespace(index=7, content="你好")
        with mock.patch.dict("os.environ", {}, clear=True):
            with self.assertRaisesRegex(RuntimeError, "requires a complete LLM"):
                translation.translate_subtitles(
                    [segment], strict=True, enable_g4f=False
                )

    def test_legacy_translation_can_still_use_source_fallback(self):
        translation = self._load_translation_with_failing_google()
        segment = SimpleNamespace(index=7, content="你好")
        with mock.patch.dict("os.environ", {}, clear=True), mock.patch.object(
            translation, "translate_with_gemini", return_value=None
        ):
            result = translation.translate_subtitles(
                [segment], strict=False, enable_g4f=False
            )
        self.assertEqual(result[0].content, "你好")

    def test_strict_translation_rejects_partial_cjk_without_machine_fallback(self):
        translation = self._load_translation_with_failing_google()

        segment = SimpleNamespace(index=3, content="你好")
        google = mock.Mock()
        with mock.patch.dict("os.environ", {}, clear=True), mock.patch.object(
            translation, "translate_with_gemini", return_value=["Xin chào 你好"]
        ), mock.patch.object(translation, "GoogleTranslator", google):
            with self.assertRaisesRegex(RuntimeError, "requires a complete LLM"):
                translation.translate_subtitles(
                    [segment], api_key="key", strict=True, enable_g4f=False
                )
        google.assert_not_called()
        self.assertEqual(segment.content, "你好")

    def test_google_error_payload_falls_back_to_mymemory(self):
        translation = self._load_translation_with_failing_google()

        class ErrorPayloadGoogleTranslator:
            def __init__(self, *args, **kwargs):
                pass

            def translate(self, text):
                return "Error 500 (Server Error)"

        class WorkingMyMemoryTranslator:
            def __init__(self, *args, **kwargs):
                pass

            def translate(self, text):
                return "Xin chào"

        segment = SimpleNamespace(index=23, content="你好")
        with mock.patch.object(
            translation, "GoogleTranslator", ErrorPayloadGoogleTranslator
        ), mock.patch.object(
            translation, "MyMemoryTranslator", WorkingMyMemoryTranslator
        ):
            result = translation.translate_subtitles(
                [segment], strict=False, enable_g4f=False
            )
        self.assertEqual(result[0].content, "Xin chào")


class ResumeVoiceConsistencyTests(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def _job(root, voice_source="edge"):
        return ResumableVideoJob(
            job_id="job",
            manifest_path=root / "job" / "pipeline_v2" / "job_manifest.json",
            job_directory=root / "job",
            source_path=root / "source.mp4",
            output_path=root / "output.mp4",
            delivery_copy_path=None,
            target_lang="vi",
            voice_source=voice_source,
            voice_param="vi-VN-HoaiMyNeural",
            clean_audio_hint=None,
            delogo=False,
            next_stage="tts",
        )

    async def test_resume_preserves_voice_provider(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            observed = {}

            class FakeRunner:
                def __init__(self, request):
                    observed["request"] = request

                async def run(self):
                    return "done"

            with mock.patch(
                "backend.pipeline_v2.resume.VideoPipelineRunner", FakeRunner
            ):
                result = await resume_video_job(
                    self._job(root),
                    PipelineSettings(mode=PipelineMode.V2),
                )

            self.assertEqual(result, "done")
            self.assertEqual(observed["request"].voice_source, "edge")


class TTSFailureSemanticsTests(unittest.IsolatedAsyncioTestCase):
    async def test_strict_fpt_does_not_silently_switch_to_edge(self):
        from datetime import timedelta
        from backend.pipeline_v2.tts import generate_tts_audio_v2

        class FakeQuotaError(Exception):
            pass

        async def fail_fpt(*_args, **_kwargs):
            raise FakeQuotaError("quota")

        async def unexpected_edge(*_args, **_kwargs):
            raise AssertionError("Edge fallback must not run in strict v2 mode")

        voice_module = ModuleType("ai.voice_cloning")
        voice_module.FPTQuotaError = FakeQuotaError
        voice_module.generate_tts_fpt = fail_fpt
        voice_module.generate_tts_edge = unexpected_edge
        voice_module._run_capcut_tts = lambda *_args, **_kwargs: None
        segment = RuntimeSegment(
            index=1,
            start=timedelta(seconds=0),
            end=timedelta(seconds=1),
            content="Xin chào",
        )
        with tempfile.TemporaryDirectory() as directory, mock.patch.dict(
            sys.modules, {"ai.voice_cloning": voice_module}
        ):
            with self.assertRaisesRegex(RuntimeError, "refusing silent provider"):
                await generate_tts_audio_v2(
                    [segment],
                    directory,
                    voice_source="fpt",
                    api_key="test-key",
                    strict_provider=True,
                )


if __name__ == "__main__":
    unittest.main()
