import unittest
from pathlib import Path
from unittest import mock

from backend import video_utils


class _FakeFFmpegChain:
    def __init__(self, captured):
        self.captured = captured

    def output(self, filename, **kwargs):
        self.captured["output"] = filename
        self.captured["output_options"] = kwargs
        return self

    def overwrite_output(self):
        return self

    def compile(self):
        return ["ffmpeg", "-version"]


class VideoUtilsPathTests(unittest.TestCase):
    def test_extract_audio_converts_path_objects_for_ffmpeg_python(self):
        captured = {}

        def fake_input(filename):
            captured["input"] = filename
            return _FakeFFmpegChain(captured)

        with mock.patch.object(video_utils.ffmpeg, "input", side_effect=fake_input), mock.patch.object(
            video_utils.subprocess, "run"
        ) as run:
            self.assertTrue(
                video_utils.extract_audio_from_video(
                    Path("input.mp4"), Path("output.wav")
                )
            )

        self.assertIsInstance(captured["input"], str)
        self.assertIsInstance(captured["output"], str)
        run.assert_called_once()


if __name__ == "__main__":
    unittest.main()
