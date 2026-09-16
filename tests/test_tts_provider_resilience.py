import tempfile
import unittest
from pathlib import Path
from unittest import mock

from backend.ai import voice_cloning


class EdgeTTSRetryTests(unittest.IsolatedAsyncioTestCase):
    async def test_transient_failures_are_retried_with_a_fresh_client(self):
        attempts = []

        class FakeCommunicate:
            def __init__(self, *_args, **_kwargs):
                pass

            async def save(self, output_path):
                attempts.append(output_path)
                if len(attempts) < 3:
                    raise RuntimeError("temporary provider failure")
                Path(output_path).write_bytes(b"a" * 256)

        with tempfile.TemporaryDirectory() as directory, mock.patch.object(
            voice_cloning.edge_tts, "Communicate", FakeCommunicate
        ):
            output = Path(directory) / "speech.mp3"
            await voice_cloning.generate_tts_edge(
                "Xin chao", str(output), attempts=3, retry_delays=(0, 0)
            )

        self.assertEqual(len(attempts), 3)

    async def test_terminal_failure_removes_partial_audio(self):
        class FailingCommunicate:
            def __init__(self, *_args, **_kwargs):
                pass

            async def save(self, output_path):
                Path(output_path).write_bytes(b"partial")
                raise RuntimeError("offline")

        with tempfile.TemporaryDirectory() as directory, mock.patch.object(
            voice_cloning.edge_tts, "Communicate", FailingCommunicate
        ):
            output = Path(directory) / "speech.mp3"
            with self.assertRaisesRegex(RuntimeError, "failed after 2 attempts"):
                await voice_cloning.generate_tts_edge(
                    "Xin chao", str(output), attempts=2, retry_delays=(0,)
                )
            self.assertFalse(output.exists())


class CapCutTTSRetryTests(unittest.TestCase):
    def test_transient_task_failure_is_retried(self):
        calls = []

        def fake_once(*_args, **_kwargs):
            calls.append(1)
            if len(calls) < 3:
                raise RuntimeError("temporary CapCut failure")
            return True

        with tempfile.TemporaryDirectory() as directory, mock.patch.object(
            voice_cloning, "_run_capcut_tts_once", side_effect=fake_once
        ):
            result = voice_cloning._run_capcut_tts(
                "Xin chao",
                str(Path(directory) / "speech.mp3"),
                attempts=3,
                retry_delays=(0, 0),
            )

        self.assertTrue(result)
        self.assertEqual(len(calls), 3)


if __name__ == "__main__":
    unittest.main()
