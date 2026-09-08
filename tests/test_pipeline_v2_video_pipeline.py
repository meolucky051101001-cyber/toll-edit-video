import tempfile
import unittest
import sys
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from backend.pipeline_v2.config import PipelineMode, PipelineSettings, QCGatePolicy
from backend.pipeline_v2.qc import evaluate_qc_gate
from backend.pipeline_v2.segments import RuntimeSegment, segments_to_dicts
from backend.pipeline_v2.video_pipeline import (
    VideoPipelineRequest,
    VideoPipelineRunner,
    compose_srt,
)


class FakeVideoPipelineRunner(VideoPipelineRunner):
    def __init__(self, request):
        super().__init__(request)
        self.calls = []

    async def _extract_audio_stage(self):
        self.calls.append("extract_audio")
        return [self.artifact_store.put_bytes("audio/original.wav", b"audio")]

    async def _demucs_stage(self):
        self.calls.append("demucs")
        return [
            self.artifact_store.put_bytes("audio/vocals.wav", b"voice"),
            self.artifact_store.put_bytes("audio/background.wav", b"background"),
        ]

    async def _transcribe_stage(self):
        self.calls.append("transcribe")
        segments = [
            RuntimeSegment(
                index=1,
                start=timedelta(seconds=0),
                end=timedelta(seconds=1),
                content="你好",
            )
        ]
        return [
            self.artifact_store.put_text("transcript/original.srt", compose_srt(segments)),
            self.artifact_store.put_json(
                "transcript/segments.json", {"segments": segments_to_dicts(segments)}
            ),
        ]

    async def _ocr_stage(self, transcript):
        self.calls.append("ocr")
        payload = segments_to_dicts(transcript)
        payload[0]["y_pct"] = 0.8
        payload[0]["max_y_pct"] = 0.85
        return [
            self.artifact_store.put_json(
                "ocr/result.json",
                {
                    "segments": payload,
                    "width": 720,
                    "height": 1280,
                    "main_y_pct": 0.8,
                },
            )
        ]

    async def _translate_stage(self, transcript):
        self.calls.append("translate")
        translated = [
            RuntimeSegment(
                index=1,
                start=timedelta(seconds=0),
                end=timedelta(seconds=1),
                content="Xin chào",
                orig_content="你好",
            )
        ]
        return [
            self.artifact_store.put_json(
                "translation/segments.json",
                {"segments": segments_to_dicts(translated)},
            ),
            self.artifact_store.put_text(
                "translation/translated.srt", compose_srt(translated)
            ),
        ]

    async def _tts_stage(self, segments):
        self.calls.append("tts")
        audio = self.artifact_store.put_bytes("tts/1.mp3", b"tts")
        index = self.artifact_store.put_json(
            "tts/segments.json",
            {
                "segments": [
                    {
                        "index": 1,
                        "source_segment_id": 1,
                        "artifact_key": "tts/1.mp3",
                        "path": None,
                        "start": 0.0,
                        "end": 1.0,
                        "actual_audio_duration": 0.9,
                        "timing_fits": True,
                        "content": "Xin chào",
                    }
                ]
            },
        )
        return [audio, index]

    async def _subtitles_stage(self, segments):
        self.calls.append("subtitles")
        return [self.artifact_store.put_text("subtitles/final.ass", "ASS")]

    async def _mix_legacy_stage(self):
        self.calls.append("mix_legacy")
        return [self.artifact_store.put_bytes("audio/mixed_legacy.wav", b"mix")]

    async def _render_stage(self):
        self.calls.append("render")
        return [self.artifact_store.put_bytes("output/final.mp4", b"final-video")]

    async def _qc_stage(self, segments):
        self.calls.append("qc")
        return [
            self.artifact_store.put_json(
                "qc/qc_report.json",
                {
                    "mode": "report_only",
                    "blocking": False,
                    "delivery_allowed": True,
                    "checks": [],
                },
            )
        ]


class VideoPipelineEndToEndTests(unittest.IsolatedAsyncioTestCase):
    async def test_rvc_index_and_adaptive_hint_participate_in_cache_identity(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.mp4"
            source.write_bytes(b"source-video")
            model = root / "voice.pth"
            index = root / "voice.index"
            model.write_bytes(b"model" * 400)
            index.write_bytes(b"index-a" * 200)
            settings = PipelineSettings(
                mode=PipelineMode.V2,
                enable_stage_cache=True,
            )
            request = VideoPipelineRequest(
                video_path=source,
                job_directory=root / "job",
                output_path=root / "delivered.mp4",
                settings=settings,
                voice_source="rvc",
                voice_param=str(model),
                rvc_model_path=model,
                clean_audio_hint=True,
            )
            first = VideoPipelineRunner(request)._load_or_create_manifest()
            first_index_hash = first.fingerprints.model_sha256["rvc_index"]

            index.write_bytes(b"index-b" * 200)
            second = VideoPipelineRunner(request)._load_or_create_manifest()
            self.assertNotEqual(
                first_index_hash,
                second.fingerprints.model_sha256["rvc_index"],
            )

            changed_hint = VideoPipelineRequest(
                video_path=source,
                job_directory=root / "hint-job",
                output_path=root / "hint-output.mp4",
                settings=settings,
                clean_audio_hint=False,
            )
            unchanged_hint = VideoPipelineRequest(
                video_path=source,
                job_directory=root / "other-hint-job",
                output_path=root / "other-hint-output.mp4",
                settings=settings,
                clean_audio_hint=True,
            )
            self.assertNotEqual(
                VideoPipelineRunner(changed_hint)
                ._load_or_create_manifest()
                .fingerprints.config_sha256,
                VideoPipelineRunner(unchanged_hint)
                ._load_or_create_manifest()
                .fingerprints.config_sha256,
            )

    async def test_pipeline_version_change_invalidates_existing_manifest(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.mp4"
            source.write_bytes(b"source-video")
            request = VideoPipelineRequest(
                video_path=source,
                job_directory=root / "job",
                output_path=root / "delivered.mp4",
                settings=PipelineSettings(
                    mode=PipelineMode.V2,
                    enable_stage_cache=True,
                ),
            )

            with mock.patch(
                "backend.pipeline_v2.video_pipeline.PIPELINE_IMPLEMENTATION_VERSION",
                "test-version-a",
            ):
                first = VideoPipelineRunner(request)._load_or_create_manifest()

            with mock.patch(
                "backend.pipeline_v2.video_pipeline.PIPELINE_IMPLEMENTATION_VERSION",
                "test-version-b",
            ):
                second = VideoPipelineRunner(request)._load_or_create_manifest()

            self.assertNotEqual(
                first.fingerprints.config_sha256,
                second.fingerprints.config_sha256,
            )
            self.assertEqual(
                second.metadata["pipeline_implementation_version"],
                "test-version-b",
            )
            archived = list(
                (root / "job" / "pipeline_v2").glob("job_manifest.*.json")
            )
            self.assertEqual(len(archived), 1)

    async def test_tts_stage_rewrites_from_measured_duration_and_persists_text(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.mp4"
            source.write_bytes(b"source-video")
            request = VideoPipelineRequest(
                video_path=source,
                job_directory=root / "job",
                output_path=root / "delivered.mp4",
                api_key="test-key",
                settings=PipelineSettings(
                    mode=PipelineMode.V2,
                    enable_timing_solver=True,
                ),
            )
            runner = VideoPipelineRunner(request)
            runner.manifest = runner._load_or_create_manifest()
            segment = RuntimeSegment(
                index=1,
                start=timedelta(seconds=0),
                end=timedelta(seconds=1),
                content="Câu lồng tiếng thực tế quá dài",
                source_segment_id=5,
            )
            calls = []

            async def fake_generate(segments, output_directory, **kwargs):
                self.assertTrue(kwargs["strict_provider"])
                calls.append(str(segments[0].content))
                output_root = Path(output_directory)
                output_root.mkdir(parents=True, exist_ok=True)
                audio = output_root / "1.mp3"
                audio.write_bytes("audio-{}".format(len(calls)).encode("ascii"))
                fits = len(calls) > 1
                return [
                    {
                        "index": 1,
                        "source_segment_id": 5,
                        "path": str(audio),
                        "start": 0.0,
                        "end": 1.0,
                        "actual_audio_duration": 0.9 if fits else 1.5,
                        "timing_fits": fits,
                        "content": str(segments[0].content),
                    }
                ]

            rewriter = mock.Mock(return_value={1: "Câu ngắn"})
            with mock.patch(
                "backend.pipeline_v2.video_pipeline.generate_tts_audio_v2",
                side_effect=fake_generate,
            ), mock.patch(
                "backend.pipeline_v2.video_pipeline.GeminiTimingRewriter",
                return_value=rewriter,
            ):
                await runner._tts_stage([segment])

            payload = runner._load_json("tts/segments.json")
            self.assertEqual(calls, ["Câu lồng tiếng thực tế quá dài", "Câu ngắn"])
            self.assertEqual(payload["runtime_segments"][0]["content"], "Câu ngắn")
            self.assertEqual(payload["unresolved_source_ids"], [])

    async def test_render_stage_converts_paths_for_legacy_renderer(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.mp4"
            source.write_bytes(b"source-video")
            request = VideoPipelineRequest(
                video_path=source,
                job_directory=root / "job",
                output_path=root / "delivered.mp4",
                settings=PipelineSettings(mode=PipelineMode.V2),
            )
            runner = VideoPipelineRunner(request)
            runner.manifest = runner._load_or_create_manifest()
            runner.artifact_store.put_text("subtitles/final.ass", "ASS")
            runner.artifact_store.put_bytes("audio/mixed_legacy.wav", b"mix")
            captured = {}

            def fake_process_video(*args):
                captured["args"] = args
                Path(args[3]).write_bytes(b"rendered")
                return True

            fake_module = SimpleNamespace(process_video=fake_process_video)
            with mock.patch.dict(sys.modules, {"video_utils": fake_module}):
                records = await runner._render_stage()

            self.assertEqual(len(records), 1)
            self.assertTrue(
                runner.artifact_store.path_for("output/final.mp4").is_file()
            )
            for path_argument in captured["args"][:4]:
                self.assertIsInstance(path_argument, str)
            self.assertGreater(float(captured["args"][9]), 0.0)

    async def test_fake_pipeline_delivers_and_resumes_without_rerunning(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.mp4"
            source.write_bytes(b"source-video")
            output = root / "delivered.mp4"
            settings = PipelineSettings(
                mode=PipelineMode.V2,
                enable_gpu_process_isolation=False,
                enable_stage_cache=True,
            )
            request = VideoPipelineRequest(
                video_path=source,
                job_directory=root / "job",
                output_path=output,
                settings=settings,
            )
            first = FakeVideoPipelineRunner(request)
            self.assertEqual(first.gpu_executor.lock_path, root / "pipeline_v2_gpu.lock")
            result = await first.run()
            self.assertEqual(output.read_bytes(), b"final-video")
            self.assertTrue(result.qc_allowed)
            self.assertEqual(
                first.calls,
                [
                    "extract_audio",
                    "demucs",
                    "transcribe",
                    "ocr",
                    "translate",
                    "tts",
                    "subtitles",
                    "mix_legacy",
                    "render",
                    "qc",
                ],
            )

            resumed = FakeVideoPipelineRunner(request)
            await resumed.run()
            self.assertEqual(resumed.calls, [])

            # A delivery destination change reuses expensive media artifacts
            # but must publish to the newly requested path.
            second_output = root / "delivered-second.mp4"
            second_request = VideoPipelineRequest(
                video_path=source,
                job_directory=root / "job",
                output_path=second_output,
                settings=settings,
            )
            redirected = FakeVideoPipelineRunner(second_request)
            await redirected.run()
            self.assertEqual(redirected.calls, [])
            self.assertEqual(second_output.read_bytes(), b"final-video")

            # Corrupting a completed artifact invalidates that stage and only
            # its downstream consumers; earlier expensive stages stay cached.
            resumed.artifact_store.path_for("translation/segments.json").write_bytes(
                b"corrupt"
            )
            repaired = FakeVideoPipelineRunner(request)
            await repaired.run()
            self.assertNotIn("extract_audio", repaired.calls)
            self.assertNotIn("demucs", repaired.calls)
            self.assertNotIn("transcribe", repaired.calls)
            self.assertNotIn("ocr", repaired.calls)
            self.assertIn("translate", repaired.calls)
            self.assertIn("render", repaired.calls)

    async def test_progress_callback_failure_does_not_fail_media_pipeline(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.mp4"
            source.write_bytes(b"source-video")

            def broken_progress(_stage, _state):
                raise ConnectionError("status channel offline")

            request = VideoPipelineRequest(
                video_path=source,
                job_directory=root / "job",
                output_path=root / "delivered.mp4",
                settings=PipelineSettings(mode=PipelineMode.V2),
                progress=broken_progress,
            )
            runner = FakeVideoPipelineRunner(request)
            result = await runner.run()
            self.assertTrue(result.final_video.is_file())
            self.assertTrue(runner.manifest.metadata["progress_warnings"])

    async def test_parallel_context_rejects_empty_artifacts_and_resets_corrupt_cache(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.mp4"
            source.write_bytes(b"source-video")
            request = VideoPipelineRequest(
                video_path=source,
                job_directory=root / "job",
                output_path=root / "delivered.mp4",
                settings=PipelineSettings(mode=PipelineMode.V2),
            )
            runner = VideoPipelineRunner(request)
            runner.manifest = runner._load_or_create_manifest()

            for name in ("ocr", "translate"):
                runner.manifest.start_stage(name)
                artifact = runner.artifact_store.put_bytes(
                    "{}/stale.bin".format(name), b"stale"
                )
                runner.manifest.complete_stage(name, [artifact])
                runner.artifact_store.path_for(artifact.key).write_bytes(b"corrupt")

            async def empty_stage(_transcript):
                return []

            with mock.patch.object(runner, "_ocr_stage", side_effect=empty_stage), mock.patch.object(
                runner, "_translate_stage", side_effect=empty_stage
            ):
                with self.assertRaisesRegex(RuntimeError, "produced no artifacts"):
                    await runner._execute_parallel_context([])

            self.assertEqual(runner.manifest.stage("ocr").status.value, "failed")
            self.assertEqual(runner.manifest.stage("translate").status.value, "failed")

    async def test_progress_warning_history_is_bounded(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.mp4"
            source.write_bytes(b"source-video")

            def broken_progress(_stage, _state):
                raise ConnectionError("status channel offline")

            runner = VideoPipelineRunner(
                VideoPipelineRequest(
                    video_path=source,
                    job_directory=root / "job",
                    output_path=root / "delivered.mp4",
                    settings=PipelineSettings(mode=PipelineMode.V2),
                    progress=broken_progress,
                )
            )
            runner.manifest = runner._load_or_create_manifest()
            for _ in range(75):
                await runner._notify("tts", "running")

            self.assertEqual(len(runner.manifest.metadata["progress_warnings"]), 50)
            self.assertEqual(runner.manifest.metadata["progress_warning_count"], 75)

    async def test_runner_rejects_destructive_output_and_invalid_rvc(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.mp4"
            source.write_bytes(b"source-video")
            with self.assertRaisesRegex(ValueError, "must not overwrite"):
                VideoPipelineRunner(
                    VideoPipelineRequest(
                        video_path=source,
                        job_directory=root / "job-a",
                        output_path=source,
                        settings=PipelineSettings(mode=PipelineMode.V2),
                    )
                )
            with self.assertRaisesRegex(ValueError, "real .pth"):
                VideoPipelineRunner(
                    VideoPipelineRequest(
                        video_path=source,
                        job_directory=root / "job-b",
                        output_path=root / "out.mp4",
                        settings=PipelineSettings(mode=PipelineMode.V2),
                        voice_source="rvc",
                        voice_param=str(root / "missing.pth"),
                        rvc_model_path=root / "missing.pth",
                    )
                )
            with self.assertRaisesRegex(ValueError, "FPT voice requires"):
                VideoPipelineRunner(
                    VideoPipelineRequest(
                        video_path=source,
                        job_directory=root / "job-c",
                        output_path=root / "out-fpt.mp4",
                        settings=PipelineSettings(mode=PipelineMode.V2),
                        voice_source="fpt",
                        voice_param="banmai",
                    )
                )


class QCGateTests(unittest.TestCase):
    def test_block_policy_blocks_errors_but_report_only_does_not(self):
        report = {"checks": [{"name": "video_stream", "status": "error"}]}
        blocked = evaluate_qc_gate(report, QCGatePolicy.BLOCK)
        allowed = evaluate_qc_gate(report, QCGatePolicy.REPORT_ONLY)
        self.assertFalse(blocked.allowed)
        self.assertTrue(allowed.allowed)


if __name__ == "__main__":
    unittest.main()
