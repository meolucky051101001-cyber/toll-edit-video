"""Regression tests for model changes, real stage wiring and desktop settings."""
import asyncio
import importlib.util
import os
import sys
import tempfile
import unittest
from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from backend.environment import read_environment, load_environment
from backend.model_workers.model_runtime_worker import _run_qwen_asr
from backend.pipeline_v2.config import PipelineSettings
from backend.pipeline_v2.content import merge_ocr_geometry
from backend.pipeline_v2.resume import find_resumable_jobs, resume_video_job
from backend.pipeline_v2.segments import RuntimeSegment, segments_to_dicts
from backend.pipeline_v2.timing import solve_segment_timing
from backend.pipeline_v2.tts import generate_tts_audio_v2
from backend.pipeline_v2.video_pipeline import VideoPipelineRequest, VideoPipelineRunner


def segment(text="你好", gender="male"):
    return RuntimeSegment(1, timedelta(), timedelta(seconds=1), text, gender=gender)


class PipelineRegressionTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        source = self.root / "source.mp4"
        source.write_bytes(b"source A")
        self.request = VideoPipelineRequest(
            source, self.root / "job", self.root / "output.mp4",
            PipelineSettings(enable_stage_cache=True),
        )

    def runner(self):
        runner = VideoPipelineRunner(self.request)
        runner.manifest = runner._load_or_create_manifest()
        return runner

    async def test_translation_resume_and_model_provider_source_changes(self):
        calls = []

        def translate(batch, *args, **kwargs):
            calls.append(1)
            batch[0].orig_content = batch[0].content
            batch[0].content = "Bản dịch {}".format(len(calls))
            return batch

        module = SimpleNamespace(translate_subtitles=translate)
        with mock.patch.dict(sys.modules, {"ai.translation": module}), mock.patch.dict(
            os.environ, {"GEMINI_MODEL": "model-A", "LLM_PROVIDER": "gemini"}
        ):
            first = self.runner()
            await first._translate_stage([segment()])
            resumed = self.runner()
            await resumed._translate_stage([segment()])
            self.assertEqual(len(calls), 1)
            self.assertEqual(first._batch_scope, resumed._batch_scope)

            for key, value in (("GEMINI_MODEL", "model-B"), ("LLM_PROVIDER", "deepseek")):
                os.environ[key] = value
                changed = self.runner()
                await changed._translate_stage([segment()])
            self.assertEqual(len(calls), 3)
            self.request.video_path.write_bytes(b"source B with same transcript")
            changed = self.runner()
            await changed._translate_stage([segment()])
            self.assertEqual(len(calls), 4)
            self.assertEqual(changed._load_segments("translation/segments.json")[0].content, "Bản dịch 4")

    async def test_disabled_cache_does_not_restore_completed_batch(self):
        self.request = replace(self.request, settings=replace(self.request.settings, enable_stage_cache=False))
        first = self.runner()
        first.manifest.start_stage("deliver")
        first.manifest.complete_stage("deliver")
        first.manifest_store.save(first.manifest)
        original_scope = first._batch_scope
        first.manifest = first._load_or_create_manifest()
        self.assertNotEqual(original_scope, first._batch_scope)
        self.assertEqual(first._batch_scope, self.runner()._batch_scope)

    async def test_real_transcribe_stage_preserves_gender_through_timing(self):
        self.request = replace(self.request, settings=replace(self.request.settings, enable_auto_gender=True))
        runner = self.runner()

        def gpu(stage, payload, *args):
            Path(payload["output_srt"]).write_text("1\n00:00:00,000 --> 00:00:01,000\n你好\n", encoding="utf-8")
            return {"segments": segments_to_dicts([segment()])}

        def detect(segments, audio):
            segments[0].gender = "male"
            return segments

        with mock.patch.object(runner, "_speech_audio", return_value=self.request.video_path), mock.patch.object(
            runner.gpu_executor, "run", side_effect=gpu
        ), mock.patch("backend.pipeline_v2.gender_detector.enrich_segments_with_gender", side_effect=detect) as detector:
            await runner._transcribe_stage()
            detector.assert_called_once()
        restored = runner._load_segments("transcript/segments.json")
        restored[0].content = "Một câu rất dài, phần thứ hai cũng dài, phần cuối vẫn dài."
        solved = solve_segment_timing(merge_ocr_geometry(restored, restored))
        self.assertGreater(len(solved.segments), 1)
        self.assertTrue(all(item.gender == "male" for item in solved.segments))

    async def test_fixed_voice_asr_does_not_require_gender_detector(self):
        runner = self.runner()

        def gpu(stage, payload, *args):
            Path(payload["output_srt"]).write_text("srt", encoding="utf-8")
            return {"segments": segments_to_dicts([segment()])}

        with mock.patch.object(runner, "_speech_audio", return_value=self.request.video_path), mock.patch.object(
            runner.gpu_executor, "run", side_effect=gpu
        ), mock.patch.dict(sys.modules, {"backend.pipeline_v2.gender_detector": None}):
            await runner._transcribe_stage()
        self.assertEqual(len(runner._load_segments("transcript/segments.json")), 1)

    async def test_style_is_written_to_ass_and_preserved_on_resume(self):
        self.request = replace(self.request, font_name="Tahoma", font_color="&H0000FF00", font_weight=1)
        runner = self.runner()
        await runner._subtitles_stage([segment("Xin chào")])
        ass = runner.artifact_store.path_for("subtitles/final.ass").read_text(encoding="utf-8-sig")
        self.assertIn("Style: TextStyle,Tahoma,38,&H0000FF00", ass)
        self.assertIn("&H00000000,0,0,0,0,100,100", ass)
        jobs = find_resumable_jobs(self.root)
        self.assertEqual(len(jobs), 1)
        with mock.patch("backend.pipeline_v2.resume.VideoPipelineRunner") as factory:
            factory.return_value.run = mock.AsyncMock(return_value="resumed")
            await resume_video_job(jobs[0], self.request.settings)
            resumed_request = factory.call_args.args[0]
            self.assertEqual((resumed_request.font_name, resumed_request.font_color, resumed_request.font_weight),
                             ("Tahoma", "&H0000FF00", 1))
        self.request = replace(self.request, font_color="&H000000FF")
        self.assertNotEqual(runner._batch_scope, self.runner()._batch_scope)

    async def test_fpt_selection_survives_auto_gender(self):
        voices = []

        async def fpt(text, path, key, voice):
            voices.append(voice)
            Path(path).write_bytes(b"tts")

        module = SimpleNamespace(FPTQuotaError=RuntimeError,
            _run_capcut_tts=mock.Mock(side_effect=AssertionError("wrong provider")),
            generate_tts_edge=mock.AsyncMock(side_effect=AssertionError("wrong provider")), generate_tts_fpt=fpt)

        def fit(source, output, duration, policy):
            Path(output).write_bytes(b"fitted")
            return SimpleNamespace(source_duration_seconds=duration, target_duration_seconds=duration,
                output_duration_seconds=duration, applied_atempo=1.0, fits=True)

        with mock.patch.dict(sys.modules, {"ai.voice_cloning": module}), mock.patch(
            "backend.pipeline_v2.tts.fit_audio_to_window", side_effect=fit
        ):
            await generate_tts_audio_v2([segment()], self.root / "tts", voice_source="fpt",
                voice_param="leminh", api_key="test-only", strict_provider=True, enable_auto_gender=True)
        self.assertEqual(voices, ["leminh"])


class EnvironmentTests(unittest.TestCase):
    def test_bom_quotes_and_process_precedence_match_preflight(self):
        from backend.pipeline_v2.preflight import _with_dotenv
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / ".env").write_text('GEMINI_MODEL=file-model\nFONT="A # B"\nPIPELINE_MODE=v2 # local\n', encoding="utf-8-sig")
            values = read_environment(root, {"GEMINI_MODEL": "process-model"})
            self.assertEqual(values, {"GEMINI_MODEL": "process-model", "FONT": "A # B", "PIPELINE_MODE": "v2"})
            self.assertEqual(values, _with_dotenv({"GEMINI_MODEL": "process-model"}, root))
            with mock.patch.dict(os.environ, {"GEMINI_MODEL": "process-model"}, clear=True):
                load_environment(root)
                self.assertEqual(os.environ["GEMINI_MODEL"], "process-model")
                self.assertEqual(os.environ["PIPELINE_MODE"], "v2")


class QwenOverlapTests(unittest.TestCase):
    def test_overlap_words_belong_to_one_chunk_without_clipping(self):
        class Audio:
            def __init__(self, length): self.length = length
            def __len__(self): return self.length
            def __getitem__(self, item): return Audio(item.stop - item.start)

        class Model:
            calls = 0
            def transcribe(self, **kwargs):
                offset = 0 if self.calls == 0 else 239.25
                self.calls += 1
                words = [(239.4, 239.6, "before"), (239.7, 239.9, "after")]
                return [SimpleNamespace(language="Chinese", time_stamps=[
                    {"start_time": start - offset, "end_time": end - offset, "text": text}
                    for start, end, text in words])]

        modules = {
            "librosa": SimpleNamespace(load=lambda *args, **kwargs: (Audio(241 * 16000), 16000)),
            "torch": SimpleNamespace(cuda=SimpleNamespace(is_available=lambda: False), float32="float32"),
            "qwen_asr": SimpleNamespace(Qwen3ASRModel=SimpleNamespace(from_pretrained=lambda *args, **kwargs: Model())),
        }
        with mock.patch.dict(sys.modules, modules):
            result = _run_qwen_asr({"input_audio": "stub.wav", "model_name": "stub", "aligner_name": "stub"})
        self.assertEqual([item["text"] for item in result["timestamps"]], ["before", "after"])
        self.assertEqual([(item["start"], item["end"]) for item in result["timestamps"]], [(239.4, 239.6), (239.7, 239.9)])


class ApiStyleTests(unittest.IsolatedAsyncioTestCase):
    async def test_api_request_passes_selected_font_into_pipeline(self):
        backend = Path(__file__).resolve().parents[1] / "backend"
        sys.path.insert(0, str(backend))
        self.addCleanup(sys.path.remove, str(backend))
        with tempfile.TemporaryDirectory() as directory, mock.patch.dict(os.environ, {
            "AUTODUB_WORKSPACE": directory, "AUTODUB_OUTPUT_DIR": directory, "PIPELINE_MODE": "v2"
        }):
            spec = importlib.util.spec_from_file_location("review_api_main", backend / "main.py")
            api = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(api)
            with mock.patch("pipeline_v2.video_pipeline.VideoPipelineRunner") as factory:
                factory.return_value.run = mock.AsyncMock(return_value="done")
                await api.run_api_pipeline_v2("input.mp4", directory, "out.mp4", "copy.mp4", "vi", "edge",
                    "vi-VN-HoaiMyNeural", "", "Tahoma", "&H0000FF00", 2)
                request = factory.call_args.args[0]
                self.assertEqual((request.font_name, request.font_color, request.font_weight), ("Tahoma", "&H0000FF00", 2))


if __name__ == "__main__":
    unittest.main()
