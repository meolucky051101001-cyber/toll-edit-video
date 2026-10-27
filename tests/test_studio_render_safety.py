"""Hồi quy P0/P1 Studio: SRT canonical + atomic, overlay-only chain, batch fallback, vocal resolver."""
import base64
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

BASE_DIR = Path(__file__).resolve().parent.parent / "backend"
sys.path.insert(0, str(BASE_DIR))

import workflow_api as wf  # noqa: E402


def _write_srt(path: Path, rows):
    blocks = [f"{i}\n{start} --> {end}\n{text}\n" for i, (start, end, text) in enumerate(rows, 1)]
    path.write_text("\n".join(blocks), encoding="utf-8")


def _read_srt_text(path: Path):
    return [(s["start"], s["end"], s["text"]) for s in wf.parse_srt(path.read_text(encoding="utf-8"))]


RAW_TRANSLATED = [
    ("00:00:00,000", "00:00:02,000", "Một"),
    ("00:00:02,000", "00:00:04,000", "Hai"),
    ("00:00:04,000", "00:00:06,000", "Ba"),
]
RAW_ORIGINAL = [
    ("00:00:00,000", "00:00:02,000", "一"),
    ("00:00:02,000", "00:00:04,000", "二"),
    ("00:00:04,000", "00:00:06,000", "三"),
]


class TestSrtCanonicalRetime(unittest.TestCase):
    def _apply(self, job, input_dir, downloads, thumbnails, payload):
        with patch.object(wf, "WORKSPACE", job.parent), \
             patch.object(wf, "INPUT_DIR", input_dir), \
             patch.object(wf, "DOWNLOADS_DIR", downloads), \
             patch.object(wf, "THUMBNAILS_DIR", thumbnails), \
             patch.object(wf, "_apply_timeline_edits_file", return_value=True), \
             patch.object(wf, "_valid_final_video", return_value=True), \
             patch.object(wf.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, "", "")):
            return wf._execute_apply_edits(payload)

    def test_raw_rerender_uses_canonical_source_not_retimed_file(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            input_dir = root / "input"
            downloads = root / "downloads"
            thumbnails = root / "thumbs"
            for d in (input_dir, downloads, thumbnails):
                d.mkdir()
            (input_dir / "vid1.mp4").write_bytes(b"x" * 2048)
            job = root / "workspace" / "vid1"
            job.mkdir(parents=True)
            _write_srt(job / "translated.srt", RAW_TRANSLATED)
            _write_srt(job / "original.srt", RAW_ORIGINAL)

            clips = [{"start": 0, "end": 3}, {"start": 5, "end": 6}]
            first = self._apply(job, input_dir, downloads, thumbnails,
                                {"stem": "vid1", "clips": clips, "target_version": "raw"})
            first_rows = _read_srt_text(job / "translated.srt")
            self.assertEqual(len(first_rows), 3)
            self.assertEqual([r[2] for r in first_rows], ["Một", "Hai", "Ba"])
            self.assertTrue((job / "translated.source.srt").is_file())
            self.assertTrue((job / "translated.prev.srt").is_file())
            self.assertTrue((job / "original.source.srt").is_file())
            self.assertEqual([r.get("text") for r in first["subtitles"]], ["Một", "Hai", "Ba"])

            # Render lại từ phôi với đúng clips cũ: không được retime chồng lên bản đã retime
            second = self._apply(job, input_dir, downloads, thumbnails,
                                 {"stem": "vid1", "clips": clips, "target_version": "raw"})
            self.assertEqual(_read_srt_text(job / "translated.srt"), first_rows)
            self.assertEqual([r.get("text") for r in second["subtitles"]], ["Một", "Hai", "Ba"])
            self.assertEqual(_read_srt_text(job / "translated.source.srt"),
                             [(0.0, 2.0, "Một"), (2.0, 4.0, "Hai"), (4.0, 6.0, "Ba")])

    def test_final_render_retimes_from_current_timeline_and_keeps_canonical(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            input_dir = root / "input"
            downloads = root / "downloads"
            thumbnails = root / "thumbs"
            for d in (input_dir, downloads, thumbnails):
                d.mkdir()
            (input_dir / "vid2.mp4").write_bytes(b"x" * 2048)
            job = root / "workspace" / "vid2"
            job.mkdir(parents=True)
            _write_srt(job / "translated.srt", RAW_TRANSLATED)
            _write_srt(job / "original.srt", RAW_ORIGINAL)

            self._apply(job, input_dir, downloads, thumbnails,
                        {"stem": "vid2", "clips": [{"start": 0, "end": 3}, {"start": 5, "end": 6}],
                         "target_version": "raw"})
            canonical_before = _read_srt_text(job / "translated.source.srt")

            (job / "final_vid2.mp4").write_bytes(b"y" * 2048)
            result = self._apply(job, input_dir, downloads, thumbnails,
                                 {"stem": "vid2", "clips": [{"start": 0, "end": 2.5}], "target_version": "final"})
            self.assertEqual(len(result["subtitles"]), 2)
            self.assertEqual(_read_srt_text(job / "translated.source.srt"), canonical_before)


class TestOverlayOnlyChain(unittest.TestCase):
    def test_overlay_without_text_reaches_outv_and_uses_shortest(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            video = base / "in.mp4"
            video.write_bytes(b"x" * 2048)
            probe = subprocess.CompletedProcess([], 0, json.dumps({"streams": [
                {"codec_type": "video", "width": 1080, "height": 1920},
                {"codec_type": "audio", "sample_rate": "44100", "channels": 2, "channel_layout": "stereo"},
            ]}), "")
            cmds = []

            def fake_run(cmd, **kwargs):
                cmds.append(cmd)
                return probe

            def fake_ffmpeg(cmd, *args, **kwargs):
                cmds.append(cmd)
                return subprocess.CompletedProcess(cmd, 1, "", "")

            src = "data:image/png;base64," + base64.b64encode(b"fake-png").decode()
            with patch.object(wf.subprocess, "run", side_effect=fake_run), \
                 patch.object(wf, "_run_ffmpeg_ctl", side_effect=fake_ffmpeg):
                ok = wf._apply_timeline_edits_file(
                    video, [{"start": 0.0, "end": 5.0}], base / "out.mp4",
                    overlay_clips=[{"src": src, "at": 0.0, "dur": 2.0}],
                    workspace_dir=base,
                )

            self.assertFalse(ok)
            ffmpeg_cmds = [c for c in cmds if c and c[0] == "ffmpeg"]
            self.assertEqual(len(ffmpeg_cmds), 2)
            for cmd in ffmpeg_cmds:
                fc = cmd[cmd.index("-filter_complex") + 1]
                self.assertIn("[vo0]null[outv]", fc)
                self.assertNotIn("[basev]null[outv]", fc)
                self.assertIn("-shortest", cmd)


class TestBatchCpuFallbackSource(unittest.TestCase):
    def test_cpu_fallback_uses_raw_source_when_mirror_with_srt(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            scan_dir = root / "scan"
            phoi_dir = root / "phoi"
            downloads = root / "downloads"
            workspace = root / "workspace"
            output_dir = root / "out"
            for d in (scan_dir, phoi_dir, downloads, workspace, output_dir):
                d.mkdir()
            v_file = scan_dir / "clip.mp4"
            v_file.write_bytes(b"x" * 2048)
            phoi = phoi_dir / "clip.mp4"
            phoi.write_bytes(b"y" * 2048)
            job = workspace / "clip"
            job.mkdir()
            _write_srt(job / "translated.srt", RAW_TRANSLATED)

            cmds = []

            def fake_run(cmd, **kwargs):
                cmds.append(cmd)
                nvenc = "h264_nvenc" in cmd
                if not nvenc:
                    Path(cmd[-1]).write_bytes(b"o" * 2048)
                return subprocess.CompletedProcess(cmd, 1 if nvenc else 0, "", "")

            cfg = {
                "aspect_ratio": "9:16", "bg_type": "blur", "mirror": True,
                "auto_crop_black_bars": False, "video_scale": 1.0, "protect_top_thumb": False,
            }
            with patch.object(wf, "INPUT_DIR", phoi_dir), \
                 patch.object(wf, "DOWNLOADS_DIR", downloads), \
                 patch.object(wf, "WORKSPACE", workspace), \
                 patch.object(wf, "OUTPUT_DIR", output_dir), \
                 patch.object(wf, "batch_framing_state", {
                     "status": "idle", "current": 0, "total": 0, "percent": 0.0,
                     "completed_files": [], "error_files": [], "message": "",
                 }), \
                 patch.object(wf, "probe_stream_durations", return_value={"video": 5.0, "audio": 5.0}), \
                 patch.object(wf, "build_custom_ass_file", return_value=None), \
                 patch.object(wf, "_valid_final_video", return_value=True), \
                 patch.object(wf.subprocess, "run", side_effect=fake_run):
                wf._batch_framing_worker(scan_dir, output_dir, cfg)

            nvenc = next(c for c in cmds if "h264_nvenc" in c)
            cpu = next(c for c in cmds if "libx264" in c)
            for cmd in (nvenc, cpu):
                self.assertEqual(cmd[cmd.index("-i") + 1], str(phoi))


class TestManualRawResolver(unittest.TestCase):
    def test_accepts_job_dir_argument_and_prefers_it(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            input_dir = root / "input"
            downloads = root / "downloads"
            output_dir = root / "output"
            workspace = root / "workspace"
            for d in (input_dir, downloads, output_dir, workspace):
                d.mkdir()
            job = workspace / "unique-stem-x1"
            job.mkdir()
            target = job / "step1_extracted.mp4"
            target.write_bytes(b"z" * 2048)

            with patch.object(wf, "INPUT_DIR", input_dir), \
                 patch.object(wf, "DOWNLOADS_DIR", downloads), \
                 patch.object(wf, "OUTPUT_DIR", output_dir), \
                 patch.object(wf, "WORKSPACE", workspace):
                resolved = wf._manual_resolve_raw_video("unique-stem-x1", job)

            self.assertEqual(resolved, target)


if __name__ == "__main__":
    unittest.main()
