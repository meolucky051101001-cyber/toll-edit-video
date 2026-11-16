"""Frozen contract of the unrefactored execute_job; all heavy engines are mocked."""
import copy
import asyncio
import importlib
import json
import os
import tempfile
import unittest
import wave
from contextlib import ExitStack, asynccontextmanager
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch


FIXTURES = Path(__file__).parent / "fixtures" / "phase7"


class OrchestratorGoldenTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / "fixture.mp4"
        self.source.write_bytes(b"source fixture" * 2000)
        self.output = self.root / "work"
        self.delivery = self.root / "delivery" / "Dubbed_fixture.mp4"
        self.events = []
        self.fail_mix = False
        self.block_qc = False

    def wav(self, path, seconds=4):
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        with wave.open(str(path), "wb") as stream:
            stream.setnchannels(1)
            stream.setsampwidth(2)
            stream.setframerate(16000)
            stream.writeframes(b"\0" * (32000 * seconds))
        return path

    def snapshot(self):
        return {
            "events": self.events,
            "artifacts": sorted(p.relative_to(self.output).as_posix() for p in self.output.rglob("*") if p.is_file()),
            "translation": json.loads((self.output / "translation.json").read_text(encoding="utf-8")),
            "ass": (self.output / "final.ass").read_text(encoding="utf-8-sig"),
        }

    def harness(self):
        import srt
        import ass_utils
        import ocr_utils
        import video_utils
        import video_pause
        import job_tracker
        import stop_service
        import disk_guard
        import dashboard_media
        import render_history
        import workspace_cleaner
        import v1_gpu_gatekeeper
        import v1_quality_gate
        from backend.v1_orchestrator import V1Orchestrator
        from backend.v1_feature_flags import get_feature_flags
        from backend.ai import v1_asr_isolated, v1_voice_isolated, v1_auto_voice, translation

        self.orchestrator = V1Orchestrator(self.root)
        flags = get_feature_flags(self.root)
        flags["V1_SMART_SKIP_OCR"] = False
        config = {
            "policy_version": 2, "voice_mode": "manual", "voice_config": {"enabled": False},
            "feature_flags": flags, "visual_sub_to_speech": getattr(self, "visual_mode", False), "script_mode": "default",
            "target_lang": "vi", "font_name": "Arial", "font_color": "&H00000000", "font_weight": 2,
            "planned_pipeline": {"asr_model": "whisper_turbo", "asr_chunking": False,
                                 "asr_chunk_size_s": 240, "asr_overlap_s": 2,
                                 "separation_model": "roformer", "mixer_mode": "direct", "ocr_strategy": "full"},
        }
        self.orchestrator.manifest_manager.get_or_create_manifest(
            "golden_fixture", self.source, "SHORT", effective_config=config,
            routing_snapshot={"metadata": {"duration_s": 4.0}, "requested_mode": "SHORT"},
        )
        manager = self.orchestrator.manifest_manager
        real_start = manager.record_stage_start
        real_ass = ass_utils.generate_ass_file
        real_reload = importlib.reload
        import pipeline_v2.atomic_io as atomic_io
        real_copy = atomic_io.atomic_copy_file

        def start(job_id, stage):
            self.events.append("start:" + stage)
            return real_start(job_id, stage)

        def extract(source, output):
            self.events.append("extract")
            self.wav(output)
            return True

        def separate(source, output, **kwargs):
            self.events.append("separate")
            return str(self.wav(Path(output) / "vocals.wav")), str(self.wav(Path(output) / "no_vocals.wav"))

        def asr(source, output, **kwargs):
            self.events.append("asr")
            segments = [srt.Subtitle(i, timedelta(seconds=start), timedelta(seconds=end), text)
                        for i, start, end, text in [(1, .2, 1.4, "原文一"), (2, 1.8, 3.2, "原文二")]]
            Path(output).write_text(srt.compose(segments), encoding="utf-8")
            return segments

        def ocr(source, **kwargs):
            self.events.append("ocr")
            for segment in kwargs["srt_segments"]:
                segment.ocr_mask_status = "no_caption_evidence"
                segment.best_block = None
                segment.tracking_blocks = []
            return [], 1080, 1920, .88

        def translate(segments, *args, **kwargs):
            self.events.append("translate")
            result = copy.deepcopy(segments)
            for segment, text in zip(result, ["Câu thử một.", "Câu thử hai."]):
                segment.content = text
            return result

        def voice(**kwargs):
            self.events.append("voice")
            payload = {"voice_source": "fixture", "voice_param": "fixture", "voice_label": "Fixture"}
            (self.output / "voice_lock.json").write_text(json.dumps(payload), encoding="utf-8")
            return payload

        async def tts(segments, directory, **kwargs):
            self.events.append("tts")
            return [{"index": s.index, "path": str(self.wav(Path(directory) / f"{s.index}.wav", seconds=1)),
                     "duration": 1.0, "actual_audio_duration": 1.0} for s in segments]

        def ass(*args, **kwargs):
            self.events.append("ass")
            return real_ass(*args, **kwargs)

        def mix(source, dubs, output, **kwargs):
            self.events.append("mix")
            if self.fail_mix:
                raise RuntimeError("fixture mixer interruption")
            self.wav(output)

        def render(source, subtitles, audio, output, **kwargs):
            self.events.append("render")
            Path(output).write_bytes(b"rendered fixture" * 2000)
            return True

        def qc(**kwargs):
            self.events.append("qc")
            self.assertEqual(kwargs["policy"], "BLOCK")
            if self.block_qc:
                raise RuntimeError("Quality Gate BLOCK: fixture")
            return SimpleNamespace(overall_status="PASSED", to_dict=lambda: {"overall_status": "PASSED"})

        def deliver(source, destination):
            self.events.append("deliver")
            return real_copy(source, destination)

        @asynccontextmanager
        async def gate(*args, **kwargs):
            yield

        stack = ExitStack()
        self.addCleanup(stack.close)
        replacements = [
            (self.orchestrator, "plan_job", lambda *a, **k: {}),
            (self.orchestrator, "_check_gpu_guard", lambda *a: None),
            (manager, "record_stage_start", start),
            (video_utils, "extract_audio_from_video", extract), (video_utils, "separate_vocals_demucs", separate),
            (v1_asr_isolated, "extract_subtitles_isolated", asr), (ocr_utils, "perform_video_ocr", ocr),
            (ocr_utils, "release_ocr_reader", lambda: None), (translation, "translate_subtitles", translate),
            (v1_auto_voice, "decide_video_voice", voice), (v1_voice_isolated, "generate_dubbing_audio_isolated", tts),
            (ass_utils, "generate_ass_file", ass), (ass_utils, "subtitle_measure", lambda *a: lambda t: len(t)*20),
            (video_utils, "mix_audio_pydub", mix), (video_utils, "process_video", render),
            (v1_gpu_gatekeeper, "async_gpu_gatekeeper", gate), (v1_quality_gate, "run_quality_gate", qc),
            (video_pause, "checkpoint", AsyncMock()), (stop_service, "is_stop_requested", lambda *a, **k: False),
            (job_tracker, "is_stop_requested", lambda: False), (job_tracker, "get_status", lambda: {"translation_models": ["fixture"]}),
            (job_tracker, "finish_video", Mock()), (job_tracker, "record_router_info", Mock()),
            (job_tracker, "record_active_translation_model", Mock()), (disk_guard, "ensure_free_space", Mock()),
            (dashboard_media, "media_status", lambda *a, **k: {"status": "valid", "reason": "fixture"}),
            (render_history, "record_render_duration", Mock()),
            (workspace_cleaner, "clean_completed_job_intermediate_files", Mock()),
            (workspace_cleaner, "clean_stale_downloads", Mock()), (atomic_io, "atomic_copy_file", deliver),
            (importlib, "reload", lambda module: module if module is ass_utils or module is v1_quality_gate else real_reload(module)),
        ]
        for target, name, replacement in replacements:
            stack.enter_context(patch.object(target, name, replacement))

    async def execute(self):
        async def progress(stage, *args):
            if stage == "publish":
                self.events.append("publish")
        return await self.orchestrator.execute_job(
            self.source, "golden_fixture", output_dir=self.output, delivery_path=self.delivery,
            progress_callback=progress, stop_checker=lambda: False,
        )

    async def test_success_matches_frozen_stage_artifact_translation_and_ass_contract(self):
        self.harness()
        result = await self.execute()
        self.assertEqual(result["status"], "success")
        actual = self.snapshot()
        if os.getenv("TOOL_V1_CAPTURE_GOLDEN") == "1":
            destination = Path(__file__).parents[1] / "scratch/prephase7/golden-capture.json"
            destination.write_text(json.dumps(actual, ensure_ascii=False, indent=2), encoding="utf-8")
        expected = json.loads((FIXTURES / "execute_job.json").read_text(encoding="utf-8"))
        self.assertEqual(actual, expected)
        self.assertEqual(self.delivery.read_bytes(), (self.output / "final_fixture.mp4").read_bytes())
        self.assertEqual(self.orchestrator.manifest_manager.load_manifest("golden_fixture").status, "completed")
        self.events.clear()
        reused = await self.execute()
        self.assertTrue(reused["reused_completed"])
        self.assertEqual(self.events, [])

    async def test_mixer_interruption_resumes_without_repeating_asr_ocr_translation_or_tts(self):
        self.harness()
        self.fail_mix = True
        with self.assertRaisesRegex(RuntimeError, "fixture mixer interruption"):
            await self.execute()
        before = {name: (self.output / name).read_bytes() for name in ["translation.json", "final.ass"]}
        self.assertFalse(self.delivery.exists())
        self.fail_mix = False
        self.events.clear()
        result = await self.execute()
        self.assertEqual(result["status"], "success")
        for name, content in before.items():
            self.assertEqual((self.output / name).read_bytes(), content)
        self.assertEqual(self.events, ["start:audio_mixing", "mix", "start:video_rendering", "render", "qc", "deliver", "publish"])

    async def test_qc_block_keeps_existing_delivery_untouched_and_checkpoint_incomplete(self):
        self.harness()
        self.delivery.parent.mkdir(parents=True)
        self.delivery.write_bytes(b"existing published video")
        self.block_qc = True
        with self.assertRaisesRegex(RuntimeError, "Quality Gate BLOCK"):
            await self.execute()
        self.assertEqual(self.delivery.read_bytes(), b"existing published video")
        self.assertNotIn("deliver", self.events)
        self.assertNotIn("publish", self.events)
        self.assertNotEqual(self.orchestrator.manifest_manager.load_manifest("golden_fixture").status, "completed")

    async def test_visual_transcript_reuses_ocr_from_asr_and_keeps_original_bgm(self):
        import srt
        import ocr_utils
        import video_utils
        from backend.ai import v1_asr_isolated

        self.visual_mode = True
        self.harness()

        def empty_asr(source, output, **kwargs):
            self.events.append("asr")
            Path(output).write_text("", encoding="utf-8")
            return []

        def visual_ocr(source, **kwargs):
            self.events.append("ocr")
            self.assertEqual(kwargs["srt_segments"], [])
            segments = [srt.Subtitle(i, timedelta(seconds=a), timedelta(seconds=b), text)
                        for i, a, b, text in [(1, .2, 1.7, "今天我们学习做饭"), (2, 1.8, 3.8, "现在开始准备材料")]]
            for segment in segments:
                segment.visual_sub_to_speech = True
                segment.ocr_mask_status = "no_caption_evidence"
                segment.tracking_blocks = []
                segment.best_block = None
            return segments, 1080, 1920, .88

        real_mix = video_utils.mix_audio_pydub
        with patch.object(v1_asr_isolated, "extract_subtitles_isolated", empty_asr), \
             patch.object(ocr_utils, "perform_video_ocr", side_effect=visual_ocr) as ocr, \
             patch.object(ocr_utils, "release_ocr_reader") as release, \
             patch.object(video_utils, "mix_audio_pydub", side_effect=real_mix) as mix:
            result = await self.execute()
        self.assertEqual(result["status"], "success")
        ocr.assert_called_once()
        release.assert_called_once()
        self.assertEqual(Path(mix.call_args.args[0]), self.output / "original.wav")
        self.assertEqual(self.events.count("ocr"), 1)
        self.assertIn("今天我们学习做饭", (self.output / "original.srt").read_text(encoding="utf-8"))

    async def test_visual_resume_recomputes_old_geometry_instead_of_reusing_sidecar(self):
        import srt
        import ocr_utils
        from backend.ai import v1_asr_isolated
        from backend.v1_stage_runtime import load_payload, save_payload
        from backend.v1_ocr_geometry import OCR_GEOMETRY_VERSION

        self.visual_mode = True
        self.harness()
        self.fail_mix = True

        def empty_asr(source, output, **kwargs):
            self.events.append("asr")
            Path(output).write_text("", encoding="utf-8")
            return []

        def visual_ocr(source, **kwargs):
            self.events.append("ocr")
            self.assertEqual(kwargs["srt_segments"], [])
            seg = srt.Subtitle(1, timedelta(seconds=.2), timedelta(seconds=3.8), "今天我们学习做饭")
            seg.visual_sub_to_speech = True
            seg.ocr_mask_status = "no_caption_evidence"
            seg.tracking_blocks, seg.best_block = [], None
            return [seg], 1080, 1920, .88

        with patch.object(v1_asr_isolated, "extract_subtitles_isolated", empty_asr), \
             patch.object(ocr_utils, "perform_video_ocr", side_effect=visual_ocr):
            with self.assertRaisesRegex(RuntimeError, "fixture mixer interruption"):
                await self.execute()
            sidecar = self.output / "ocr.json"
            cached = load_payload(sidecar)
            cached["geometry_version"] = OCR_GEOMETRY_VERSION - 1
            save_payload(sidecar, **cached)
            self.orchestrator.manifest_manager.record_stage_complete(
                "golden_fixture", "visual_ocr", {"ocr_data": str(sidecar)})
            self.fail_mix = False
            self.events.clear()
            result = await self.execute()
        self.assertEqual(result["status"], "success")
        self.assertEqual(self.events.count("ocr"), 1)
        self.assertNotIn("asr", self.events)
        self.assertIn("translate", self.events)
        self.assertEqual(load_payload(sidecar)["geometry_version"], OCR_GEOMETRY_VERSION)

    async def test_progress_cancellation_stops_before_first_heavy_stage(self):
        self.harness()

        async def cancel(*args):
            raise asyncio.CancelledError()

        with self.assertRaises(asyncio.CancelledError):
            await self.orchestrator.execute_job(
                self.source, "golden_fixture", output_dir=self.output, delivery_path=self.delivery,
                progress_callback=cancel, stop_checker=lambda: False,
            )
        self.assertEqual(self.events, [])
        self.assertFalse(self.delivery.exists())
        self.assertNotEqual(self.orchestrator.manifest_manager.load_manifest("golden_fixture").status, "completed")
