import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from backend.ai import source_separation


def model_policy(backend="auto"):
    return SimpleNamespace(
        separator_backend=backend,
        separator_model="model_bs_roformer_ep_317_sdr_12.9755.ckpt",
        model_cache_directory="C:/models",
        demucs_primary_model="htdemucs_ft",
        demucs_fallback_model="htdemucs",
    )


class SourceSeparationRefactorTests(unittest.TestCase):
    def test_auto_policy_returns_bs_roformer_outputs_before_demucs(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.wav"
            vocals = root / "roformer" / "vocals.wav"
            background = root / "roformer" / "instrumental.wav"
            source.write_bytes(b"source")
            vocals.parent.mkdir()
            vocals.write_bytes(b"vocals")
            background.write_bytes(b"background")

            with mock.patch.object(
                source_separation, "current_model_policy", return_value=model_policy()
            ), mock.patch.object(
                source_separation, "runtime_module_available", return_value=True
            ), mock.patch.object(
                source_separation,
                "run_model_stage",
                return_value={
                    "vocals_path": str(vocals),
                    "background_path": str(background),
                    "effective_precision": "native_fp16",
                },
            ) as run_model, mock.patch.object(
                source_separation.subprocess, "run"
            ) as run_demucs, mock.patch("builtins.print"):
                result = source_separation.separate_vocals(source, root / "output")

            self.assertEqual(result, (str(vocals), str(background)))
            self.assertEqual(run_model.call_args.args[0], "separator")
            run_demucs.assert_not_called()

    def test_demucs_policy_preserves_fine_tuned_then_fallback_order(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.wav"
            output = root / "output"
            source.write_bytes(b"source")
            commands = []

            def fake_demucs(command, **_kwargs):
                commands.append(command)
                model_name = command[command.index("-n") + 1]
                model_output = output / model_name / source.stem
                model_output.mkdir(parents=True)
                (model_output / "vocals.wav").write_bytes(b"vocals")
                (model_output / "no_vocals.wav").write_bytes(b"background")

            with mock.patch.object(
                source_separation,
                "current_model_policy",
                return_value=model_policy("demucs"),
            ), mock.patch.object(
                source_separation.subprocess, "run", side_effect=fake_demucs
            ), mock.patch("builtins.print"):
                result = source_separation.separate_vocals(source, output)

            demucs_command = next(
                command
                for command in commands
                if isinstance(command, list) and "demucs" in command
            )
            self.assertEqual(
                demucs_command[demucs_command.index("-n") + 1], "htdemucs_ft"
            )
            self.assertTrue(Path(result[0]).is_file())
            self.assertTrue(Path(result[1]).is_file())


if __name__ == "__main__":
    unittest.main()
