"""Local batch contracts with temporary media and mocked heavy stages."""
import asyncio
import copy
import tempfile
import unittest
import wave
from contextlib import ExitStack, asynccontextmanager
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch


class LocalBatchContracts(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        from backend import batch_processor as batch
        import batch_checkpoint
        import job_config_service
        import v1_feature_flags
        import v1_orchestrator
        from pipeline_v2.config import PipelineMode, PipelineSettings

        self.batch = batch
        self.orch_module = v1_orchestrator
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / "fixture.mp4"
        self.source.write_bytes(b"input fixture" * 2000)
        self.workspace = self.root / "workspace"
        self.job = self.workspace / "fixture"
        self.output = self.root / "delivery"
        self.output.mkdir()
        self.delivered = self.output / "Dubbed_fixture.mp4"
        self.events = []
        self.flags = {"V1_USE_ORCHESTRATOR": True}
        self.voice_map = {"1": {"source": "capcut", "param": "fixture-voice"}}
        self.frozen = {"effective_config": {"voice_mode": "manual", "voice_id": "fixture-voice",
                        "segment_voices": self.voice_map, "visual_sub_to_speech": False}}
        self.fake_orch = SimpleNamespace(execute_job=AsyncMock())
        self.tracker = SimpleNamespace(**{name: Mock() for name in [
            "start_video", "update_step", "finish_video", "set_error", "mark_stopped",
            "record_active_translation_model",
        ]}, get_status=Mock(return_value={"active": False, "translation_models": ["fixture"]}))

        async def subtitles(kind, source, produce, output):
            return await produce()

        stack = ExitStack()
        self.addCleanup(stack.close)
        for owner, name, value in [
            (batch, "WORKSPACE", str(self.workspace)), (batch, "job_tracker", self.tracker),
            (batch, "pause_checkpoint", AsyncMock()), (batch, "_raise_if_stopped", Mock()),
            (batch, "_cleanup_job_directory", lambda *a: self.events.append("cleanup")),
            (batch_checkpoint, "Checkpoints", Mock(return_value=SimpleNamespace(subtitles=subtitles))),
            (PipelineSettings, "from_env", Mock(return_value=SimpleNamespace(mode=PipelineMode.LEGACY))),
            (v1_feature_flags, "get_feature_flags", lambda *a: self.flags),
            (job_config_service, "get_frozen_config", lambda *a, **k: self.frozen),
            (v1_orchestrator, "V1Orchestrator", Mock(return_value=self.fake_orch)),
        ]:
            stack.enter_context(patch.object(owner, name, value))

    async def execute(self):
        return await self.batch.process_single_local_video(
            str(self.source), str(self.output), job_id="fixture-job", work_dir=str(self.job))

    async def test_orchestrator_success_records_receipt_without_finishing_tracker_twice(self):
        import json

        self.delivered.write_bytes(b"published fixture")
        self.fake_orch.execute_job.return_value = {
            "final_video": str(self.delivered), "video_mode": "SHORT",
            "qc_status": "PASSED", "tracker_finalized": True,
        }
        with patch.object(self.batch, "extract_audio_from_video") as extract:
            self.assertTrue(await self.execute())
        extract.assert_not_called()
        self.tracker.finish_video.assert_not_called()
        request = self.fake_orch.execute_job.call_args.kwargs
        self.assertEqual(request["job_id"], "fixture-job")
        self.assertEqual(request["delivery_path"], str(self.delivered))
        self.assertTrue(request["resume_if_possible"])
        receipt = json.loads(self.batch._receipt_path(str(self.delivered)).read_text(encoding="utf-8"))
        self.assertEqual(receipt["pipeline_version"], "v1_orchestrator_complete")
        self.assertEqual(receipt["qc_status"], "PASSED")

    async def test_orchestrator_failure_keeps_checkpoint_and_never_starts_legacy(self):
        self.fake_orch.execute_job.side_effect = RuntimeError("fixture failure")
        with patch.object(self.batch, "extract_audio_from_video") as extract:
            self.assertFalse(await self.execute())
        extract.assert_not_called()
        self.tracker.set_error.assert_called_once()
        self.assertNotIn("cleanup", self.events)

    async def test_orchestrator_cancellation_propagates(self):
        self.fake_orch.execute_job.side_effect = asyncio.CancelledError()
        with patch.object(self.batch, "extract_audio_from_video") as extract:
            with self.assertRaises(asyncio.CancelledError):
                await self.execute()
        extract.assert_not_called()
        self.tracker.set_error.assert_not_called()
        self.assertNotIn("cleanup", self.events)

    async def test_legacy_preserves_stage_order_voice_map_and_delivery(self):
        import srt
        import ass_utils
        import voice_selection
        import v1_gpu_gatekeeper
        import torch

        self.flags["V1_USE_ORCHESTRATOR"] = False
        source_cues = [srt.Subtitle(1, timedelta(), timedelta(seconds=2), "原文测试")]

        def wav(path):
            path = Path(path)
            path.parent.mkdir(parents=True, exist_ok=True)
            with wave.open(str(path), "wb") as stream:
                stream.setnchannels(1)
                stream.setsampwidth(2)
                stream.setframerate(16000)
                stream.writeframes(b"\0" * 64000)
            return str(path)

        def extract(source, output):
            self.events.append("extract")
            wav(output)
            return True

        def separate(source, output, **kwargs):
            self.events.append("separate")
            return wav(Path(output) / "vocals.wav"), wav(Path(output) / "no_vocals.wav")

        def asr(source, output, **kwargs):
            self.events.append("asr")
            Path(output).write_text(srt.compose(source_cues), encoding="utf-8")
            return copy.deepcopy(source_cues)

        def ocr(*args, **kwargs):
            self.events.append("ocr")
            return [], 1080, 1920, .88

        def translate(cues, *args, **kwargs):
            self.events.append("translate")
            cues = copy.deepcopy(cues)
            cues[0].content = "Câu thử."
            return cues

        async def dub(cues, directory, **kwargs):
            self.events.append("tts")
            return [{"index": 1, "path": wav(Path(directory) / "1.wav"), "duration": 2.0}]

        def ass(cues, floating, output, **kwargs):
            self.events.append("ass")
            Path(output).write_text("fixture ASS")

        def mix(source, dubs, output, **kwargs):
            self.events.append("mix")
            wav(output)

        def render(source, subtitles, audio, output, **kwargs):
            self.events.append("render")
            Path(output).write_bytes(b"rendered fixture")
            return True

        @asynccontextmanager
        async def gate(*args, **kwargs):
            yield

        with ExitStack() as stack:
            for owner, name, value in [
                (self.batch, "extract_audio_from_video", extract),
                (self.batch, "separate_vocals_demucs", separate),
                (self.batch, "extract_subtitles_whisper", asr),
                (self.batch, "perform_video_ocr", ocr), (self.batch, "release_ocr_reader", Mock()),
                (self.batch, "translate_subtitles", translate),
                (self.batch, "generate_dubbing_audio_isolated", AsyncMock(side_effect=dub)),
                (self.batch, "generate_ass_file", ass), (self.batch, "mix_audio_pydub", mix),
                (self.batch, "process_video", render),
                (self.batch, "probe_downloaded_video", Mock(return_value=SimpleNamespace(audio_stream_count=1))),
                (voice_selection, "catalog", lambda: [{"id": "fixture-voice", "source": "capcut", "param": "fixture-voice", "label": "Fixture"}]),
                (ass_utils, "sync_and_clamp_subtitles", lambda cues, dubs: cues),
                (v1_gpu_gatekeeper, "async_gpu_gatekeeper", gate),
                (torch.cuda, "is_available", lambda: False),
            ]:
                stack.enter_context(patch.object(owner, name, value))
            tts = self.batch.generate_dubbing_audio_isolated
            self.assertTrue(await self.execute())
            self.assertEqual(tts.call_args.kwargs["segment_voices"], self.voice_map)
            self.assertEqual(tts.call_args.kwargs["voice_source"], "capcut")
        self.assertEqual(self.events, ["extract", "separate", "asr", "ocr", "translate", "tts", "ass", "mix", "render", "cleanup"])
        self.assertEqual(self.delivered.read_bytes(), b"rendered fixture")
        self.tracker.finish_video.assert_called_once()
        self.tracker.set_error.assert_not_called()
