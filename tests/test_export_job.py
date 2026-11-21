"""Hồi quy nhóm 3: export chạy nền có progress/cancel, không phá file cũ, waveform cache."""
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import os
from pathlib import Path
from unittest.mock import patch

import numpy as np

BASE_DIR = Path(__file__).resolve().parent.parent / "backend"
sys.path.insert(0, str(BASE_DIR))

import workflow_api
import main as main_app
from main import app
from fastapi.testclient import TestClient


def _make_tiny_video(path: Path) -> None:
    subprocess.run([
        "ffmpeg", "-y", "-v", "error",
        "-f", "lavfi", "-i", "testsrc=size=270x480:rate=30:duration=0.3",
        "-f", "lavfi", "-i", "sine=frequency=440:duration=0.3",
        "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-shortest", str(path),
    ], check=True)


def _make_tiny_wav(path: Path) -> None:
    subprocess.run([
        "ffmpeg", "-y", "-v", "error",
        "-f", "lavfi", "-i", "sine=frequency=660:duration=0.3",
        "-c:a", "pcm_s16le", str(path),
    ], check=True)


def _wait_status(client, job_id, timeout=30.0):
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        res = client.get("/api/workflow/export-status", params={"job_id": job_id})
        if res.status_code != 200:
            raise AssertionError(res.text)
        last = res.json()
        if last.get("status") in ("done", "error", "cancelled"):
            return last
        time.sleep(0.1)
    raise AssertionError(f"Export job chưa kết thúc sau {timeout}s: {last}")


class ExportJobTestBase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        root = Path(self._tmp.name)
        self.workspace = root / "workspace"
        self.input_dir = root / "input"
        self.downloads = root / "downloads"
        self.output_dir = root / "output"
        self.thumbs = root / "thumbs"
        for d in (self.workspace, self.input_dir, self.downloads, self.output_dir, self.thumbs):
            d.mkdir(parents=True, exist_ok=True)
        for name, value in {
            "WORKSPACE": self.workspace,
            "INPUT_DIR": self.input_dir,
            "DOWNLOADS_DIR": self.downloads,
            "OUTPUT_DIR": self.output_dir,
            "THUMBNAILS_DIR": self.thumbs,
        }.items():
            patcher = patch.object(workflow_api, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.client = TestClient(app, headers={"X-Local-Control-Token": main_app.LOCAL_TOKEN})
        with workflow_api._EXPORT_JOBS_LOCK:
            workflow_api._EXPORT_JOBS.clear()

    def _payload(self, **overrides):
        payload = {
            "stem": "vid",
            "resolution": "720p",
            "bitrate_kbps": 500,
            "fps": "30",
            "format": "mp4",
            "export_srt": False,
            "output_folder": str(self.output_dir),
            "prepend_thumbnail": False,
            "canvas_settings": {
                "aspect_ratio": "9:16",
                "video_scale": 1.0,
                "mirror": False,
                "bg_type": "image",
                "motion_bg_enabled": False,
                "auto_crop_black_bars": False,
                "protect_top_thumb": False,
                "crop_padding": 0,
                "bg_color": "#f59e0b",
            },
        }
        payload.update(overrides)
        return payload


class ExportJobApiTests(ExportJobTestBase):
    def test_export_runs_as_job_and_writes_output(self):
        _make_tiny_video(self.input_dir / "vid.mp4")
        res = self.client.post("/api/workflow/export-custom", json=self._payload())
        self.assertEqual(res.status_code, 200, res.text)
        body = res.json()
        self.assertTrue(body.get("job_id"))
        self.assertEqual(body.get("status"), "ok")

        final = _wait_status(self.client, body["job_id"])
        self.assertEqual(final["status"], "done", final)
        self.assertEqual(final["progress"], 100)
        result = final["result"]
        out_file = Path(result["output_file"])
        self.assertTrue(out_file.is_file())
        self.assertGreater(out_file.stat().st_size, 1000)
        self.assertEqual(result["filename"], out_file.name)
        self.assertGreater(result["size_mb"], 0)

    def test_custom_export_encoder_commands_match_legacy_argv(self):
        source = self.input_dir / "vid.mp4"
        source.write_bytes(b"source")
        commands = []

        def fake_ffmpeg(cmd, job_ctl=None, timeout=900):
            commands.append(cmd)
            if "libx264" in cmd:
                Path(cmd[-1]).write_bytes(b"rendered" * 200)
                return subprocess.CompletedProcess(cmd, 0, "", "")
            return subprocess.CompletedProcess(cmd, 1, "", "")

        with patch.object(workflow_api, "probe_stream_durations",
                          return_value={"video": 1.0, "audio": 1.0}), \
             patch.object(workflow_api, "_run_ffmpeg_ctl", side_effect=fake_ffmpeg), \
             patch.object(workflow_api, "_valid_final_video",
                          side_effect=lambda path, defer=False: Path(path).is_file()), \
             patch.object(workflow_api.time, "time_ns", return_value=123):
            workflow_api._execute_custom_export(self._payload())

        temp = commands[0][-1]
        graph = (
            "[0:v]scale=720:1280:force_original_aspect_ratio=decrease,"
            "pad=720:1280:(ow-iw)/2:(oh-ih)/2,setsar=1[outv]"
        )
        common_prefix = [
            "ffmpeg", "-y", "-i", str(source), "-filter_complex", graph,
            "-map", "[outv]", "-map", "0:a?",
        ]
        common_suffix = [
            "-r", "30", "-c:a", "aac", "-b:a", "192k",
            "-movflags", "+faststart", "-shortest", str(temp),
        ]
        self.assertEqual(commands, [
            common_prefix + [
                "-c:v", "h264_nvenc", "-preset", "p4", "-b:v", "500k",
                "-maxrate", "750k", "-bufsize", "1000k",
            ] + common_suffix,
            common_prefix + [
                "-c:v", "libx264", "-preset", "medium", "-b:v", "500k",
                "-maxrate", "750k", "-bufsize", "1000k",
            ] + common_suffix,
        ])

    def test_export_tts_mix_keeps_base_audio(self):
        _make_tiny_video(self.input_dir / "vid.mp4")
        job_dir = self.workspace / "vid"
        job_dir.mkdir(parents=True, exist_ok=True)
        _make_tiny_wav(job_dir / "manual_tts_track.wav")

        res = self.client.post("/api/workflow/export-custom", json=self._payload(tts_mix=True))
        self.assertEqual(res.status_code, 200, res.text)
        final = _wait_status(self.client, res.json()["job_id"])
        self.assertEqual(final["status"], "done", final)

        out_file = Path(final["result"]["output_file"])
        self.assertTrue(out_file.is_file())
        probe = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "a",
             "-show_entries", "stream=codec_name", "-of", "csv=p=0", str(out_file)],
            capture_output=True, text=True,
        )
        self.assertTrue(probe.stdout.strip(), "Video xuất phải giữ luồng audio gốc khi trộn TTS")

    def test_export_tts_mix_graph_mixes_base_audio(self):
        (self.input_dir / "vid.mp4").write_bytes(b"fake source")
        job_dir = self.workspace / "vid"
        job_dir.mkdir()
        tts_wav = job_dir / "manual_tts_track.wav"
        tts_wav.write_bytes(b"RIFFfake")
        cmds = []

        def failing_run(cmd, job_ctl=None, timeout=900):
            cmds.append(cmd)
            return subprocess.CompletedProcess(cmd, 1, "", "stop")

        with patch.object(workflow_api, "_run_ffmpeg_ctl", side_effect=failing_run), \
                patch.object(workflow_api, "probe_stream_durations",
                             return_value={"video": 1.0, "audio": 1.0}):
            with self.assertRaises(workflow_api.HTTPException):
                workflow_api._execute_custom_export(self._payload(tts_mix=True))

        self.assertEqual(len(cmds), 2)
        for cmd in cmds:
            self.assertIn(str(tts_wav), cmd)
            fc = cmd[cmd.index("-filter_complex") + 1]
            self.assertIn("[0:a]aformat=", fc)
            self.assertNotIn("0:aaformat", fc)
            self.assertIn("[expaout]", cmd)

    def test_cancel_queued_or_running_job(self):
        started = threading.Event()

        def blocking_export(payload, job_ctl=None):
            started.set()
            while not (job_ctl and job_ctl.get("cancelled")):
                time.sleep(0.02)
            raise workflow_api.HTTPException(status_code=499, detail="Đã huỷ xuất video")

        with patch.object(workflow_api, "_execute_custom_export", side_effect=blocking_export):
            res = self.client.post("/api/workflow/export-custom", json=self._payload())
            self.assertEqual(res.status_code, 200, res.text)
            job_id = res.json()["job_id"]
            self.assertTrue(started.wait(5), "worker export không chạy")

            cancel = self.client.post("/api/workflow/export-cancel", json={"job_id": job_id})
            self.assertEqual(cancel.status_code, 200, cancel.text)
            self.assertTrue(cancel.json()["cancelled"])

            final = _wait_status(self.client, job_id, timeout=10)
            self.assertEqual(final["status"], "cancelled", final)

    def test_export_failure_keeps_previous_file_and_removes_temp(self):
        (self.input_dir / "vid.mp4").write_bytes(b"fake source")
        old_file = self.output_dir / "CapCut_vid_720p_500k.mp4"
        old_file.write_bytes(b"OLD-EXPORT")

        def failing_run(cmd, job_ctl=None, timeout=900):
            return subprocess.CompletedProcess(cmd, 1, "", "ffmpeg failed")

        with patch.object(workflow_api, "_run_ffmpeg_ctl", side_effect=failing_run):
            with self.assertRaises(workflow_api.HTTPException):
                workflow_api._execute_custom_export(self._payload())

        self.assertEqual(old_file.read_bytes(), b"OLD-EXPORT")
        self.assertEqual(list(self.output_dir.iterdir()), [old_file])

    def test_successful_export_publishes_temp_atomically(self):
        (self.input_dir / "vid.mp4").write_bytes(b"fake source")
        old_file = self.output_dir / "CapCut_vid_720p_500k.mp4"
        old_file.write_bytes(b"OLD-EXPORT")

        def successful_run(cmd, job_ctl=None, timeout=900):
            Path(cmd[-1]).write_bytes(b"NEW-EXPORT")
            return subprocess.CompletedProcess(cmd, 0, "", "")

        with patch.object(workflow_api, "_run_ffmpeg_ctl", side_effect=successful_run), \
                patch.object(workflow_api, "_valid_final_video", side_effect=lambda p, defer=False: Path(p).is_file()):
            result = workflow_api._execute_custom_export(self._payload())

        self.assertEqual(old_file.read_bytes(), b"NEW-EXPORT")
        self.assertEqual(list(self.output_dir.iterdir()), [old_file])
        self.assertEqual(result["filename"], old_file.name)


class WaveformCacheTests(ExportJobTestBase):
    def test_short_video_cache_hits_with_requested_buckets(self):
        (self.input_dir / "vid.mp4").write_bytes(b"fake source")
        (self.workspace / "vid").mkdir()

        samples = (np.sin(np.arange(8000) / 10.0) * 12000).astype(np.int16).tobytes()
        completed = subprocess.CompletedProcess(["ffmpeg"], 0, samples, "")

        with patch.object(workflow_api.subprocess, "run", return_value=completed) as run_mock:
            first = workflow_api._build_waveform_sync("vid", "raw", 1600)
            second = workflow_api._build_waveform_sync("vid", "raw", 1600)

        self.assertFalse(first["cached"])
        self.assertEqual(first["buckets"], 20)
        self.assertTrue(second["cached"])
        self.assertEqual(second["buckets"], 1600)
        self.assertEqual(len(second["peaks"]), 20)
        self.assertEqual(run_mock.call_count, 1)


if __name__ == "__main__":
    unittest.main()
