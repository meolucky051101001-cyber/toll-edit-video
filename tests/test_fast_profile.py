import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from backend.ai.model_policy import RuntimeModelPolicy
from backend.ai import source_separation
from backend.ai.translation import translate_with_gemini, translate_with_openai, translate_with_deepseek


class FastProfileTests(unittest.TestCase):
    def test_fast_demucs_uses_v1_parameters_and_retries_with_real_stems(self):
        # Torch performs platform subprocess probes at import time; initialize
        # before replacing subprocess.run for the separator commands.
        import torch
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.wav"
            source.write_bytes(b"source")
            commands = []

            def separate(command, **kwargs):
                commands.append(command)
                model = command[command.index("-n") + 1]
                if model == "htdemucs":
                    raise RuntimeError("primary failed")
                output = root / "out" / model / "source"
                output.mkdir(parents=True)
                (output / "vocals.wav").write_bytes(b"vocals")
                (output / "no_vocals.wav").write_bytes(b"background")

            policy = RuntimeModelPolicy.from_env({"MODEL_SPEED_PROFILE": "fast"})
            with mock.patch.object(source_separation, "current_model_policy", return_value=policy), \
                    mock.patch.object(source_separation.subprocess, "run", side_effect=separate), \
                    mock.patch("builtins.print"):
                vocals, background = source_separation.separate_vocals(source, root / "out", segment_seconds=6)
            self.assertEqual([cmd[cmd.index("-n") + 1] for cmd in commands], ["htdemucs", "htdemucs_ft"])
            for cmd in commands:
                self.assertEqual(cmd[cmd.index("--shifts") + 1], "0")
                self.assertEqual(cmd[cmd.index("--overlap") + 1], "0.1")
                self.assertEqual(cmd[cmd.index("--segment") + 1], "6")
            self.assertEqual(Path(vocals).read_bytes(), b"vocals")
            self.assertEqual(Path(background).read_bytes(), b"background")

    def test_all_translation_providers_receive_duration_budgets(self):
        budget = [{"seconds": 2.4, "max_characters": 38}]
        for translate in (translate_with_gemini, translate_with_openai, translate_with_deepseek):
            response = mock.Mock(status_code=200)
            response.json.return_value = {
                "candidates": [{"content": {"parts": [{"text": '["Xin chào"]'}]}}],
                "choices": [{"message": {"content": '["Xin chào"]'}}],
            }
            post = mock.Mock(return_value=response)
            with self.subTest(provider=translate.__name__), mock.patch.dict(translate.__globals__, {
                "requests": mock.Mock(post=post),
                "extract_video_frames_base64": mock.Mock(return_value=[]),
            }):
                result = translate(["你好"], api_key="test", duration_budgets=budget)
            self.assertEqual(result, ["Xin chào"])
            payload = json.dumps(post.call_args.kwargs["json"], ensure_ascii=False)
            self.assertIn("max_characters", payload)
            self.assertIn("2.4", payload)
            self.assertIn("38", payload)
            self.assertIn("giữ đủ ý chính", payload)

    def test_provider_voice_filtering_in_tts(self):
        import asyncio
        from datetime import timedelta
        from backend.pipeline_v2.segments import RuntimeSegment
        from backend.pipeline_v2.tts import generate_tts_audio_v2

        with tempfile.TemporaryDirectory() as td:
            seg = RuntimeSegment(
                index=1,
                start=timedelta(seconds=0),
                end=timedelta(seconds=2),
                content="Xin chào các bạn",
                gender="male",
            )

            capcut_calls = []
            def fake_capcut(text, out, voice):
                capcut_calls.append(voice)
                Path(out).write_bytes(b"dummy")

            edge_calls = []
            async def fake_edge(text, out, voice, **kwargs):
                edge_calls.append(voice)
                Path(out).write_bytes(b"dummy")

            import sys
            from backend.ai import voice_cloning as vc
            sys.modules["ai.voice_cloning"] = vc
            fake_fit = mock.Mock(fitted_path=Path(td) / "1.mp3", fits=True, actual_duration=1.0, target_duration=1.0, stretch_factor=1.0, method="fit")
            # Case 1: Incompatible Edge voice mapped for CapCut provider -> must be ignored!
            with mock.patch.object(vc, "_run_capcut_tts", side_effect=fake_capcut), \
                 mock.patch.object(vc, "generate_tts_edge", side_effect=fake_edge), \
                 mock.patch("backend.pipeline_v2.tts.fit_audio_to_window", return_value=fake_fit):
                asyncio.run(
                    generate_tts_audio_v2(
                        [seg],
                        td,
                        voice_source="capcut",
                        voice_param="BV562_streaming",
                        speaker_voice_map={"male": "vi-VN-NamMinhNeural"},
                    )
                )
            # Must NOT call CapCut with Edge voice "vi-VN-NamMinhNeural"
            self.assertNotIn("vi-VN-NamMinhNeural", capcut_calls)
            self.assertIn("BV562_streaming", capcut_calls)

            # Case 2: Compatible CapCut voice mapped -> applied
            capcut_calls.clear()
            with mock.patch.object(vc, "_run_capcut_tts", side_effect=fake_capcut), \
                 mock.patch.object(vc, "generate_tts_edge", side_effect=fake_edge), \
                 mock.patch("backend.pipeline_v2.tts.fit_audio_to_window", return_value=fake_fit):
                asyncio.run(
                    generate_tts_audio_v2(
                        [seg],
                        td,
                        voice_source="capcut",
                        voice_param="BV562_streaming",
                        speaker_voice_map={"male": "BV075_streaming"},
                    )
                )
            self.assertEqual(capcut_calls, ["BV075_streaming"])

    def test_adaptive_ocr_enabled_by_default_when_env_empty(self):
        from backend.pipeline_v2.config import PipelineSettings
        settings = PipelineSettings.from_env({})
        self.assertTrue(settings.enable_adaptive_ocr)

    def test_tts_audio_v2_records_silent_fallback(self):
        import asyncio
        from datetime import timedelta
        from backend.pipeline_v2.segments import RuntimeSegment
        from backend.pipeline_v2.tts import generate_tts_audio_v2
        import sys
        from backend.ai import voice_cloning as vc
        sys.modules["ai.voice_cloning"] = vc
        import ai
        ai.voice_cloning = vc

        seg = RuntimeSegment(
            index=1,
            start=timedelta(seconds=0),
            end=timedelta(seconds=2),
            content="Xin chào",
            gender="female",
        )
        with tempfile.TemporaryDirectory() as td:
            fake_fit = mock.Mock(
                source_duration_seconds=2.0,
                target_duration_seconds=2.0,
                output_duration_seconds=2.0,
                applied_atempo=1.0,
                fits=True,
            )
            async def fake_edge_silent(*args, **kwargs):
                Path(args[1]).write_bytes(b"silence")
                return True

            # Case A: CapCut also fails -> records is_silent_fallback = True
            with mock.patch.object(vc, "generate_tts_edge", side_effect=fake_edge_silent) as mocked_edge, \
                 mock.patch.object(vc, "_run_capcut_tts", side_effect=RuntimeError("CapCut offline")), \
                 mock.patch("backend.pipeline_v2.tts.fit_audio_to_window", return_value=fake_fit):
                infos = asyncio.run(
                    generate_tts_audio_v2(
                        [seg],
                        td,
                        voice_source="edge",
                        voice_param="vi-VN-HoaiMyNeural",
                    )
                )
                self.assertEqual(len(infos), 1)
                self.assertTrue(infos[0]["is_silent_fallback"])
                self.assertEqual(mocked_edge.call_args.kwargs.get("target_duration"), 2.0)

            # Case B: Secondary CapCut fallback succeeds -> rescues from silent fallback!
            def fake_capcut_rescue(text, out_path, voice):
                Path(out_path).write_bytes(b"x" * 512)

            with mock.patch.object(vc, "generate_tts_edge", side_effect=fake_edge_silent), \
                 mock.patch.object(vc, "_run_capcut_tts", side_effect=fake_capcut_rescue), \
                 mock.patch("backend.pipeline_v2.tts.fit_audio_to_window", return_value=fake_fit):
                infos2 = asyncio.run(
                    generate_tts_audio_v2(
                        [seg],
                        td,
                        voice_source="edge",
                        voice_param="vi-VN-HoaiMyNeural",
                    )
                )
                self.assertEqual(len(infos2), 1)
                self.assertFalse(infos2[0]["is_silent_fallback"])

    def test_pipeline_early_aborts_after_tts_on_silent_fallback(self):
        import asyncio
        from datetime import timedelta
        from backend.pipeline_v2.video_pipeline import (
            VideoPipelineRunner,
            VideoPipelineRequest,
            QCGateBlocked,
            compose_srt,
        )
        from backend.pipeline_v2.config import PipelineSettings, QCGatePolicy
        from backend.pipeline_v2.segments import RuntimeSegment, segments_to_dicts

        class EarlyAbortTestRunner(VideoPipelineRunner):
            def __init__(self, request):
                super().__init__(request)
                self.executed_stages = []

            async def _extract_audio_stage(self):
                self.executed_stages.append("extract_audio")
                return [self.artifact_store.put_bytes("audio/original.wav", b"dummy-audio")]

            async def _demucs_stage(self):
                self.executed_stages.append("demucs")
                return [
                    self.artifact_store.put_bytes("audio/vocals.wav", b"dummy-vocals"),
                    self.artifact_store.put_bytes("audio/background.wav", b"dummy-bg"),
                ]

            async def _transcribe_stage(self):
                self.executed_stages.append("transcribe")
                segs = [
                    RuntimeSegment(
                        index=1,
                        start=timedelta(seconds=0),
                        end=timedelta(seconds=2),
                        content="test segment",
                    )
                ]
                return [
                    self.artifact_store.put_text("transcript/original.srt", compose_srt(segs)),
                    self.artifact_store.put_json("transcript/segments.json", {"segments": segments_to_dicts(segs)}),
                ]

            async def _ocr_stage(self, transcript):
                self.executed_stages.append("ocr")
                return [
                    self.artifact_store.put_json(
                        "ocr/result.json",
                        {"segments": segments_to_dicts(transcript), "width": 720, "height": 1280, "main_y_pct": 0.8},
                    )
                ]

            async def _translate_stage(self, transcript):
                self.executed_stages.append("translate")
                return [
                    self.artifact_store.put_json(
                        "translation/segments.json",
                        {"segments": segments_to_dicts(transcript)},
                    ),
                    self.artifact_store.put_text("translation/translated.srt", compose_srt(transcript)),
                ]

            async def _timing_stage(self, segments):
                self.executed_stages.append("timing")
                return [
                    self.artifact_store.put_json(
                        "translation/timed_segments.json",
                        {"segments": segments_to_dicts(segments)},
                    )
                ]

            async def _tts_stage(self, segments):
                self.executed_stages.append("tts")
                audio = self.artifact_store.put_bytes("tts/1.mp3", b"silent-audio")
                index = self.artifact_store.put_json(
                    "tts/segments.json",
                    {
                        "silent_fallback_count": 1,
                        "segments": [
                            {
                                "index": 1,
                                "source_segment_id": 1,
                                "artifact_key": "tts/1.mp3",
                                "start": 0.0,
                                "end": 2.0,
                                "actual_audio_duration": 2.0,
                                "timing_fits": True,
                                "is_silent_fallback": True,
                                "content": "test segment",
                            }
                        ],
                    },
                )
                return [audio, index]

            async def _rvc_stage(self, segments):
                self.executed_stages.append("rvc")
                return []

            async def _subtitles_stage(self, segments):
                self.executed_stages.append("subtitles")
                return []

            async def _mix_v2_stage(self):
                self.executed_stages.append("mix_v2")
                return []

            async def _mix_legacy_stage(self):
                self.executed_stages.append("mix_legacy")
                return []

            async def _render_stage(self):
                self.executed_stages.append("render")
                return []

            async def _qc_stage(self, segments):
                self.executed_stages.append("qc")
                return []

        with tempfile.TemporaryDirectory() as td:
            work = Path(td)
            in_video = work / "in.mp4"
            in_video.write_bytes(b"dummy-mp4-header")
            req = VideoPipelineRequest(
                video_path=in_video,
                job_directory=work / "job",
                output_path=work / "out.mp4",
                settings=PipelineSettings(
                    qc_gate_policy=QCGatePolicy.BLOCK,
                    enable_adaptive_demucs=False,
                    enable_adaptive_ocr=False,
                    enable_timing_solver=True,
                    enable_rvc=True,
                    enable_ffmpeg_mix_v2=True,
                ),
            )
            runner = EarlyAbortTestRunner(req)
            # Force _rvc_enabled to True so RVC would normally run
            runner._rvc_enabled = lambda: True

            with self.assertRaises(QCGateBlocked) as ctx:
                asyncio.run(runner.run())

            self.assertIn("QC gate early abort: TTS stage produced 1 silent fallback segment(s)", str(ctx.exception))
            # Verify upstream stages ran up to TTS
            self.assertIn("extract_audio", runner.executed_stages)
            self.assertIn("demucs", runner.executed_stages)
            self.assertIn("transcribe", runner.executed_stages)
            self.assertIn("translate", runner.executed_stages)
            self.assertIn("timing", runner.executed_stages)
            self.assertIn("tts", runner.executed_stages)
            # CRITICAL: Verify downstream stages were NEVER executed!
            self.assertNotIn("rvc", runner.executed_stages)
            self.assertNotIn("subtitles", runner.executed_stages)
            self.assertNotIn("mix_v2", runner.executed_stages)
            self.assertNotIn("mix_legacy", runner.executed_stages)
            self.assertNotIn("render", runner.executed_stages)
            self.assertNotIn("qc", runner.executed_stages)


