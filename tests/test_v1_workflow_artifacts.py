import base64
import asyncio
import importlib
import os
import subprocess
import tempfile
import unittest
import wave
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import cv2
import numpy as np
from backend.ai import translation
from backend import workflow_api as workflow


class VisualFallbackTests(unittest.TestCase):
    def test_sampler_frames_are_encoded_when_ffmpeg_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "input.mp4"
            path.write_bytes(b"fixture")
            sampler_module = importlib.import_module(
                "backend.video_sampling" if translation.__package__.startswith("backend.") else "video_sampling"
            )
            frame = np.full((64, 80, 3), 128, dtype=np.uint8)
            with patch("subprocess.run", side_effect=OSError("ffmpeg failed")), \
                 patch.object(sampler_module, "sample_video_frames", return_value=[frame]) as sampler:
                images = translation.extract_video_frames_base64(str(path), 1, 2, 1)
            sampler.assert_called_once_with(str(path), 1, 1, 2)
            self.assertEqual(len(images), 1)
            decoded = cv2.imdecode(np.frombuffer(base64.b64decode(images[0]), dtype=np.uint8), cv2.IMREAD_COLOR)
            self.assertEqual(decoded.shape, frame.shape)

    def test_failed_jpeg_encoding_is_not_returned_as_an_image(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "input.mp4"
            path.write_bytes(b"fixture")
            sampler_module = importlib.import_module(
                "backend.video_sampling" if translation.__package__.startswith("backend.") else "video_sampling"
            )
            with patch("subprocess.run", return_value=SimpleNamespace(returncode=1, stdout=b"")), \
                 patch.object(sampler_module, "sample_video_frames", return_value=[np.zeros((2, 2, 3), dtype=np.uint8)]), \
                 patch.object(cv2, "imencode", return_value=(False, None)):
                self.assertEqual(translation.extract_video_frames_base64(str(path), 1, 2, 1), [])


class WorkflowArtifactTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.job = self.root / "input"
        self.job.mkdir()
        for key, value in [("WORKSPACE", self.root), ("OUTPUT_DIR", self.root / "delivery")]:
            p = patch.object(workflow, key, value)
            p.start()
            self.addCleanup(p.stop)

    def test_fake_mp4_and_audio_are_not_ready(self):
        (self.job / "final_input.mp4").write_bytes(b"x" * 2000)
        (self.job / "original.wav").write_bytes(b"x" * 2000)
        (self.job / "mixed.wav").write_bytes(b"x" * 2000)
        (self.job / "original.srt").write_text("invalid srt", encoding="utf-8")
        result = workflow.safe_check_job_artifacts("input")
        self.assertFalse(any(result.values()))

    def test_real_media_and_subtitles_are_ready_but_other_job_is_not(self):
        video = self.job / "final_other.mp4"
        subprocess.run(
            ["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", "testsrc2=size=160x90:rate=10",
             "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=16000",
             "-t", "1", "-c:v", "libx264", "-c:a", "aac", str(video)],
            check=True, capture_output=True, timeout=30,
        )
        self.assertFalse(workflow.safe_check_job_artifacts("input")["step6"])
        video.rename(self.job / "final_input.mp4")
        for name in ("original.wav", "mixed.wav", "no_vocals.wav"):
            with wave.open(str(self.job / name), "wb") as audio:
                audio.setnchannels(1)
                audio.setsampwidth(2)
                audio.setframerate(16000)
                audio.writeframes(b"\x00\x01" * 1600)
        for name in ("original.srt", "translated.srt"):
            (self.job / name).write_text("1\n00:00:01,125 --> 00:00:02,875\ntext\n", encoding="utf-8")
        self.assertTrue(all(workflow.safe_check_job_artifacts("input").values()))
        (self.job / "final_input.mp4").write_bytes(b"corrupt" * 500)
        self.assertFalse(workflow.safe_check_job_artifacts("input")["step6"])

    def test_srt_preserves_milliseconds_in_numeric_and_display_fields(self):
        segment = workflow.parse_srt("1\n00:00:01,125 --> 00:00:02,875\ntext\n")[0]
        self.assertEqual(segment["start"], 1.125)
        self.assertEqual(segment["end"], 2.875)
        self.assertEqual(segment["start_str"], "00:00:01,125")
        self.assertEqual(segment["end_str"], "00:00:02,875")

    def test_roformer_instrumental_stem_is_validated(self):
        stem_dir = self.job / "bs_roformer"
        stem_dir.mkdir()
        instrumental = stem_dir / "original_(Instrumental)_model_bs_roformer_ep_317_sdr_12.wav"
        with wave.open(str(instrumental), "wb") as audio:
            audio.setnchannels(1)
            audio.setsampwidth(2)
            audio.setframerate(16000)
            audio.writeframes(b"\x00\x01" * 1600)
        self.assertTrue(workflow.safe_check_job_artifacts("input")["step2"])
        instrumental.write_bytes(b"corrupt stem")
        self.assertFalse(workflow.safe_check_job_artifacts("input")["step2"])


class WorkflowVideoListTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.input_dir = self.root / "phoi"
        self.downloads_dir = self.root / "downloads"
        self.output_dir = self.root / "delivery"
        for directory in (self.input_dir, self.downloads_dir, self.output_dir):
            directory.mkdir()
        for key, value in (
            ("INPUT_DIR", self.input_dir),
            ("DOWNLOADS_DIR", self.downloads_dir),
            ("WORKSPACE", self.root / "workspace"),
            ("OUTPUT_DIR", self.output_dir),
        ):
            p = patch.object(workflow, key, value)
            p.start()
            self.addCleanup(p.stop)

    def test_listing_dedupes_stems_and_sorts_newest_first(self):
        (self.input_dir / "alpha.mp4").write_bytes(b"a" * 100)
        (self.downloads_dir / "alpha.mp4").write_bytes(b"a" * 100)
        (self.downloads_dir / "beta.mp4").write_bytes(b"b" * 200)
        (self.downloads_dir / "ignore.txt").write_text("x", encoding="utf-8")
        os.utime(self.input_dir / "alpha.mp4", (100, 100))
        os.utime(self.downloads_dir / "beta.mp4", (200, 200))

        videos = asyncio.run(workflow.api_get_workflow_videos())["videos"]

        self.assertEqual([item["stem"] for item in videos], ["beta", "alpha"])
        self.assertEqual(videos[0]["source"], "downloads")
        self.assertEqual(videos[1]["source"], "phoi")
        self.assertEqual(videos[1]["size_mb"], round(100 / (1024 * 1024), 2))
        self.assertTrue(all(item["steps"] for item in videos))
