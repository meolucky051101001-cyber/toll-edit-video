import subprocess
import tempfile
import unittest
from pathlib import Path

from backend.pipeline_v2.mixer import (
    FFmpegMixSettings,
    build_ffmpeg_mix_command,
    mix_audio_ffmpeg,
)
from backend.pipeline_v2.timing import probe_audio_duration


class FFmpegMixerTests(unittest.TestCase):
    def _tone(self, path, frequency, duration):
        subprocess.run(
            [
                "ffmpeg",
                "-hide_banner",
                "-loglevel",
                "error",
                "-f",
                "lavfi",
                "-i",
                "sine=frequency={}:duration={}".format(frequency, duration),
                str(path),
            ],
            check=True,
            timeout=30,
        )

    def test_filter_graph_contains_ducking_loudnorm_and_limiter(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            background = root / "background.wav"
            dub = root / "dub.wav"
            self._tone(background, 220, 2)
            self._tone(dub, 880, 0.5)
            command, count = build_ffmpeg_mix_command(
                background,
                [{"index": 1, "path": str(dub), "start": 0.5}],
                root / "mixed.wav",
            )
            graph = command[command.index("-filter_complex") + 1]
            self.assertEqual(count, 1)
            self.assertIn("sidechaincompress", graph)
            self.assertIn("loudnorm=I=-15.0:TP=-1.0", graph)
            self.assertIn("alimiter", graph)
            self.assertIn("adelay=500", graph)

    def test_real_mix_keeps_background_duration(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            background = root / "background.wav"
            dub = root / "dub.wav"
            output = root / "mixed.wav"
            self._tone(background, 220, 2)
            self._tone(dub, 880, 0.5)
            result = mix_audio_ffmpeg(
                background,
                [{"index": 1, "path": str(dub), "start": 0.75}],
                output,
                FFmpegMixSettings(target_lufs=-15.0, true_peak_dbtp=-1.0),
                timeout_seconds=30,
            )
            self.assertEqual(result.dub_count, 1)
            self.assertAlmostEqual(probe_audio_duration(output), 2.0, delta=0.08)

    def test_scalable_mix_uses_chunked_voice_bus_without_duration_drift(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            background = root / "background.wav"
            dub = root / "dub.wav"
            output = root / "mixed-scalable.wav"
            self._tone(background, 220, 3)
            self._tone(dub, 880, 0.2)
            dubs = [
                {
                    "index": index,
                    "path": str(dub),
                    "start": index * 0.4,
                    "end": index * 0.4 + 0.2,
                }
                for index in range(6)
            ]
            result = mix_audio_ffmpeg(
                background,
                dubs,
                output,
                FFmpegMixSettings(
                    max_inputs_per_pass=2,
                    voice_chunk_seconds=1.0,
                ),
                timeout_seconds=30,
            )
            self.assertEqual(result.dub_count, 6)
            self.assertAlmostEqual(probe_audio_duration(output), 3.0, delta=0.10)

    def test_scalable_resource_limit_rejects_non_reducing_mix_tree(self):
        with self.assertRaisesRegex(ValueError, "at least 2"):
            FFmpegMixSettings(max_inputs_per_pass=1)

    def test_missing_dub_is_rejected_instead_of_silently_dropped(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            background = root / "background.wav"
            self._tone(background, 220, 1)
            with self.assertRaises(FileNotFoundError):
                mix_audio_ffmpeg(
                    background,
                    [{"index": 1, "path": str(root / "missing.mp3"), "start": 0.0}],
                    root / "mixed.wav",
                    timeout_seconds=10,
                )


if __name__ == "__main__":
    unittest.main()
