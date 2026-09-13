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
