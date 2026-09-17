import math
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from backend.pipeline_v2.mixer import (
    FFmpegMixSettings,
    mix_audio_ffmpeg,
)
from backend.pipeline_v2.timing import probe_audio_duration


class MixerChunkedDurationTests(unittest.TestCase):
    def _create_tone(self, path: Path, frequency: int, duration_seconds: float):
        subprocess.run(
            [
                "ffmpeg",
                "-hide_banner",
                "-loglevel",
                "error",
                "-y",
                "-f",
                "lavfi",
                "-i",
                f"sine=frequency={frequency}:duration={duration_seconds:.3f}",
                "-c:a",
                "pcm_s16le",
                str(path),
            ],
            check=True,
            timeout=30,
        )

    def test_scalable_mixer_duration_matches_background_with_tight_tolerance(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            bg = root / "bg.wav"
            dub = root / "dub.wav"
            out = root / "mixed.wav"

            self._create_tone(bg, 440, 5.0)
            self._create_tone(dub, 880, 0.6)

            dubs = [
                {"index": i, "path": str(dub), "start": i * 0.8, "end": i * 0.8 + 0.6}
                for i in range(5)
            ]

            result = mix_audio_ffmpeg(
                bg,
                dubs,
                out,
                settings=FFmpegMixSettings(
                    max_inputs_per_pass=2,
                    voice_chunk_seconds=2.0,
                ),
                timeout_seconds=30,
            )

            self.assertEqual(result.dub_count, 5)
            self.assertTrue(out.is_file())
            actual_dur = probe_audio_duration(out)
            self.assertAlmostEqual(actual_dur, 5.0, delta=0.15)

    def test_insufficient_disk_space_raises_before_mixing(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            bg = root / "bg.wav"
            dub = root / "dub.wav"
            out = root / "mixed.wav"
            self._create_tone(bg, 440, 1.0)
            self._create_tone(dub, 880, 0.3)

            # Mock shutil.disk_usage to return 1MB free
            with patch("shutil.disk_usage") as mock_usage:
                from collections import namedtuple
                Usage = namedtuple("Usage", ["total", "used", "free"])
                mock_usage.return_value = Usage(total=10**9, used=10**9, free=1024 * 1024)

                with self.assertRaises(OSError) as ctx:
                    mix_audio_ffmpeg(bg, [{"index": 0, "path": str(dub), "start": 0.1}], out, timeout_seconds=10)
                self.assertIn("Insufficient disk space", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
