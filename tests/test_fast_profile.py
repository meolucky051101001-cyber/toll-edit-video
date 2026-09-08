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
