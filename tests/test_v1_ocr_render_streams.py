"""Isolated regression checks; never invoke models or process the user's jobs."""
import ast
import json
import logging
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import types
import unittest
from unittest.mock import patch

for stream in (sys.stdout, sys.stderr):
    if hasattr(stream, "reconfigure"):
        stream.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parent
CANDIDATE = Path(os.environ.get("V1_REPAIR_TEST_CODE", str(ROOT.parent / "backend")))
sys.path.insert(0, r"C:\tool v1\backend")
sys.path.insert(0, str(CANDIDATE))
import v1_media_streams as media
import v1_ocr_proxy as ocr
SCRATCH = tempfile.TemporaryDirectory(prefix="v1-render-tests-")


def metadata(index=1, width=2160, height=3840, duration=20.0, cover=True, audio=True):
    streams = [{"index": index, "codec_type": "video", "codec_name": "h264", "width": width,
                "height": height, "avg_frame_rate": "30000/1001", "duration": str(duration),
                "disposition": {"attached_pic": 0, "default": 1}}]
    if cover:
        streams.insert(0, {"index": index + 1, "codec_type": "video", "codec_name": "mjpeg",
                           "width": width, "height": height, "avg_frame_rate": "0/0",
                           "r_frame_rate": "90000/1", "disposition": {"attached_pic": 1, "default": 1}})
    if audio:
        streams.append({"index": 0, "codec_type": "audio"})
    return {"streams": streams, "format": {"duration": str(duration)}}


def renderer(runner):
    tree = ast.parse((CANDIDATE / "video_utils.py").read_text(encoding="utf-8-sig"))
    function = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "process_video")
    function.decorator_list = []
    scope = {"__file__": str(Path(SCRATCH.name) / "isolated_video_utils.py"), "__name__": "isolated_render", "__package__": "",
             "os": os, "time": time, "subprocess": subprocess, "logger": logging.getLogger("test.render"),
             "run_batch_subprocess": runner, "CREATE_NO_WINDOW": 0, "print": lambda *args, **kwargs: None}
    exec(compile(ast.Module(body=[function], type_ignores=[]), "isolated-render", "exec"), scope)
    return scope["process_video"]


class MediaTests(unittest.TestCase):
    def test_audio_first_cover_first_selects_timed_video(self):
        selected = media.select_main_video(metadata())
        self.assertEqual(selected.input_map, "0:1")
        self.assertAlmostEqual(selected.fps, 29.97002997)
        self.assertTrue(selected.has_cover)

    def test_cover_only_rejected(self):
        data = metadata()
        data["streams"] = [s for s in data["streams"] if s.get("disposition", {}).get("attached_pic")]
        with self.assertRaises(ValueError):
            media.select_main_video(data)

    def test_default_timed_stream_wins(self):
        data = metadata(cover=False)
        extra = dict(data["streams"][0], index=3, disposition={"default": 0})
        data["streams"].insert(0, extra)
        self.assertEqual(media.select_main_video(data).index, 1)

    def test_first_timed_stream_without_default(self):
        data = metadata(cover=False)
        data["streams"][0]["disposition"]["default"] = 0
        extra = dict(data["streams"][0], index=4)
        data["streams"].insert(0, extra)
        self.assertEqual(media.select_main_video(data).index, 1)

    def test_rotation_matches_display_geometry(self):
        data = metadata(width=1920, height=1080, cover=False)
        data["streams"][0]["side_data_list"] = [{"rotation": -90}]
        selected = media.select_main_video(data)
        self.assertEqual((selected.width, selected.height), (1080, 1920))

    def test_reject_extra_cover_in_output(self):
        source = media.select_main_video(metadata())
        with patch.object(media, "probe_main_video", return_value=source), self.assertRaises(ValueError):
            media.validate_video_output("unused", source)

    def test_reject_wrong_output_dimensions(self):
        source = media.select_main_video(metadata())
        output = media.select_main_video(metadata(width=1080, height=1920, cover=False))
        with patch.object(media, "probe_main_video", return_value=output), self.assertRaises(ValueError):
            media.validate_video_output("unused", source)

    def test_reject_truncated_output(self):
        source = media.select_main_video(metadata())
        output = media.select_main_video(metadata(duration=18, cover=False))
        with patch.object(media, "probe_main_video", return_value=output), self.assertRaises(ValueError):
            media.validate_video_output("unused", source)

    def test_reject_wrong_frame_rate(self):
        source = media.select_main_video(metadata())
        data = metadata(cover=False)
        data["streams"][0]["avg_frame_rate"] = "15/1"
        with patch.object(media, "probe_main_video", return_value=media.select_main_video(data)), self.assertRaises(ValueError):
            media.validate_video_output("unused", source)

    def test_reject_missing_dub_audio(self):
        source = media.select_main_video(metadata())
        output = media.select_main_video(metadata(cover=False, audio=False))
        with patch.object(media, "probe_main_video", return_value=output), self.assertRaises(ValueError):
            media.validate_video_output("unused", source, require_audio=True)

    def test_accept_valid_output(self):
        source = media.select_main_video(metadata())
        output = media.select_main_video(metadata(cover=False))
        with patch.object(media, "probe_main_video", return_value=output):
            self.assertIs(media.validate_video_output("unused", source, require_audio=True), output)


class PreparationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.source_file = Path(self.temporary.name) / "nguồn video.mp4"
        self.source_file.write_bytes(b"unchanged source")
        self.source = media.select_main_video(metadata())
        self.commands = []
        self.validate = patch.object(ocr, "validate_video_output", return_value=self.source)
        self.validate.start()
        self.addCleanup(self.validate.stop)

    def run_ok(self, command, **kwargs):
        self.commands.append((command, kwargs))
        Path(command[-1]).write_bytes(b"prepared video")
        return subprocess.CompletedProcess(command, 0, "", "")

    def test_portrait_proxy_dimensions(self):
        self.assertEqual(ocr.proxy_size(self.source), (720, 1280))

    def test_landscape_proxy_dimensions(self):
        source = media.select_main_video(metadata(width=3840, height=2160))
        self.assertEqual(ocr.proxy_size(source), (1280, 720))

    def test_standard_1080p_does_not_lose_ocr_resolution(self):
        for width,height in ((1080,1920),(1920,1080)):
            source=media.select_main_video(metadata(width=width,height=height))
            self.assertEqual(ocr.proxy_size(source),(width,height))

    def test_tall_source_preserves_readable_short_side(self):
        source=media.select_main_video(metadata(width=1080,height=2560))
        self.assertEqual(ocr.proxy_size(source)[0],720)

    def test_gpu_command_maps_main_stream_no_fps_rewrite(self):
        with patch.object(ocr, "run", side_effect=self.run_ok):
            result = ocr._prepare(self.source_file, self.source)
        command, options = self.commands[0]
        self.assertEqual(command[command.index("-map") + 1], "0:1")
        self.assertEqual(command[command.index("-c:v") + 1], "h264_nvenc")
        self.assertEqual(command[command.index("-hwaccel") + 1], "cuda")
        self.assertIn("scale_cuda=720:1280:format=yuv420p", command)
        self.assertNotIn("fps=30", " ".join(command))
        self.assertNotIn("-r", command)
        self.assertLessEqual(options["timeout"], 90)
        self.assertTrue(result.is_file())
        self.assertEqual(self.source_file.read_bytes(), b"unchanged source")

    def test_retry_uses_cache_without_transcode(self):
        with patch.object(ocr, "run", side_effect=self.run_ok):
            first = ocr._prepare(self.source_file, self.source)
            second = ocr._prepare(self.source_file, self.source)
        self.assertEqual(first, second)
        self.assertEqual(len(self.commands), 1)

    def test_changed_source_has_new_cache_key(self):
        with patch.object(ocr, "run", side_effect=self.run_ok):
            first = ocr._prepare(self.source_file, self.source)
            self.source_file.write_bytes(b"new source contents")
            second = ocr._prepare(self.source_file, self.source)
        self.assertNotEqual(first, second)

    def test_gpu_timeout_fallback_is_copy_not_cpu_encode(self):
        def run(command, **kwargs):
            if "h264_nvenc" in command:
                self.commands.append((command, kwargs))
                raise subprocess.TimeoutExpired(command, kwargs["timeout"])
            return self.run_ok(command, **kwargs)
        with patch.object(ocr, "run", side_effect=run):
            result = ocr._prepare(self.source_file, self.source)
        self.assertTrue(str(result).endswith(".stream.mp4"))
        self.assertEqual(self.commands[-1][0][self.commands[-1][0].index("-c:v") + 1], "copy")
        self.assertLessEqual(self.commands[-1][1]["timeout"], 15)
        self.assertNotIn("libx264", str(self.commands))

    def test_small_video_cover_remux_only(self):
        source = media.select_main_video(metadata(width=640, height=360))
        with patch.object(ocr, "run", side_effect=self.run_ok):
            ocr._prepare(self.source_file, source)
        self.assertEqual(len(self.commands), 1)
        self.assertIn("copy", self.commands[0][0])

    def test_small_clean_video_passthrough(self):
        source = media.select_main_video(metadata(width=640, height=360, cover=False))
        seen = []
        with patch.object(ocr, "probe_main_video", return_value=source), patch.object(ocr, "run", side_effect=AssertionError):
            result = ocr.ocr_proxy(lambda path: seen.append(path) or ([], 640, 360, 0.2))(self.source_file)
        self.assertEqual(seen, [self.source_file])
        self.assertEqual(result[1:3], (640, 360))

    def test_original_geometry_and_kwargs_preserved(self):
        with patch.object(ocr, "probe_main_video", return_value=self.source), patch.object(ocr, "run", side_effect=self.run_ok):
            seen = []
            result = ocr.ocr_proxy(lambda path, **kwargs: seen.append(kwargs) or (["block"], 720, 1280, 0.12))(
                self.source_file, ocr_strategy="full", srt_segments=["cue"])
        self.assertEqual(result, (["block"], 2160, 3840, 0.12))
        self.assertEqual(seen[0]["srt_segments"], ["cue"])

    def test_cancellation_does_not_start_remux(self):
        with patch.object(ocr, "run", side_effect=RuntimeError("Batch stop requested")), self.assertRaises(RuntimeError):
            ocr._prepare(self.source_file, self.source)

    def test_all_preparation_failures_are_not_silently_ignored(self):
        with patch.object(ocr, "run", side_effect=subprocess.CalledProcessError(1, ["ffmpeg"])), self.assertRaises(subprocess.CalledProcessError):
            ocr._prepare(self.source_file, self.source)

    def test_budget_is_bounded_for_long_video(self):
        self.assertEqual(ocr.preparation_budget(3600), 90)
        self.assertLess(ocr.preparation_budget(224), 90)


class RenderTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        root = Path(self.temporary.name)
        self.video, self.audio, self.subs, self.output = [root / name for name in ("source.mp4", "dub.wav", "sub.ass", "final.mp4")]
        self.subs.write_text("[Script Info]\n", encoding="utf-8")
        self.source = media.select_main_video(metadata())
        self.probe = patch.object(media, "probe_main_video", return_value=self.source)
        self.probe.start()
        self.addCleanup(self.probe.stop)
        self.shared = patch.dict(sys.modules, {"shared_state": types.SimpleNamespace(stop_requested=False)})
        self.shared.start()
        self.addCleanup(self.shared.stop)
        self.commands = []

    def run_ok(self, command, **kwargs):
        self.commands.append(command)
        self.output.write_bytes(b"x" * 11001)
        return subprocess.CompletedProcess(command, 0, "", "")

    def test_render_exact_stream_gpu_and_original_geometry(self):
        with patch.object(media, "validate_video_output", return_value=self.source):
            self.assertTrue(renderer(self.run_ok)(self.video, self.subs, self.audio, self.output, delogo=False))
        command = self.commands[0]
        maps = [command[i + 1] for i, token in enumerate(command) if token == "-map"]
        self.assertEqual(maps, ["0:1", "1:a:0"])
        self.assertIn("h264_nvenc", command)
        self.assertNotIn("scale=", " ".join(command))
        self.assertNotIn("-r", command)

    def test_filter_error_does_not_retry_encoder(self):
        error = "Error initializing filter delogo: Invalid argument\n" + "tail\n" * 500
        def fail(command, **kwargs):
            self.commands.append(command)
            return subprocess.CompletedProcess(command, 1, "", error)
        self.assertFalse(renderer(fail)(self.video, self.subs, self.audio, self.output))
        self.assertEqual(len(self.commands), 1)
        self.assertEqual(Path(str(self.output) + ".render-p4-error.log").read_text(), error)

    def test_nvenc_option_fallback_remains_on_gpu(self):
        def run(command, **kwargs):
            if not self.commands:
                self.commands.append(command)
                return subprocess.CompletedProcess(command, 1, "", "spatial-aq unsupported")
            return self.run_ok(command, **kwargs)
        with patch.object(media, "validate_video_output", return_value=self.source):
            self.assertTrue(renderer(run)(self.video, self.subs, self.audio, self.output))
        self.assertEqual(len(self.commands), 2)
        self.assertTrue(all(command[command.index("-c:v") + 1] == "h264_nvenc" for command in self.commands))

    def test_invalid_success_output_rejected(self):
        with patch.object(media, "validate_video_output", side_effect=ValueError("wrong duration")):
            self.assertFalse(renderer(self.run_ok)(self.video, self.subs, self.audio, self.output))

    def test_render_timeout_never_falls_back_cpu(self):
        def timeout(command, **kwargs):
            self.commands.append(command)
            raise subprocess.TimeoutExpired(command, 1)
        self.assertFalse(renderer(timeout)(self.video, self.subs, self.audio, self.output, timeout_seconds=1))
        self.assertEqual(len(self.commands), 1)

    def test_stop_does_not_spawn_ffmpeg(self):
        with patch.dict(sys.modules, {"shared_state": types.SimpleNamespace(stop_requested=True)}):
            self.assertFalse(renderer(self.run_ok)(self.video, self.subs, self.audio, self.output))
        self.assertEqual(self.commands, [])


if __name__ == "__main__":
    logging.disable(logging.CRITICAL)
    unittest.main(verbosity=2)
