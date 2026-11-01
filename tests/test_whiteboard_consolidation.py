"""Regression tests for Gói C5: canonical whiteboard_animator consolidation."""

from __future__ import annotations

import inspect
from contextlib import contextmanager
from unittest.mock import patch

import numpy as np

from backend.whiteboard_animator import WhiteboardAnimator as OuterAnimator
from backend.whiteboard_animator.animator import WhiteboardAnimator as OuterAnimatorDirect
from backend.whiteboard_animator.video_encoding import (
    encode_timing_video,
    ffmpeg_cmd,
    ffmpeg_cmd_tpad,
)
from backend.whiteboard_animator.whiteboard_animator import (
    WhiteboardAnimator as NestedShimAnimator,
)
from backend.whiteboard_animator.whiteboard_animator.animator import (
    WhiteboardAnimator as NestedShimAnimatorDirect,
)
from backend.whiteboard_animator.whiteboard_animator.video_encoding import (
    encode_timing_video as nested_encode_timing_video,
    ffmpeg_cmd as nested_ffmpeg_cmd,
    ffmpeg_cmd_tpad as nested_ffmpeg_cmd_tpad,
)


def test_canonical_and_nested_shim_resolve_to_single_implementation():
    assert OuterAnimator is OuterAnimatorDirect
    assert NestedShimAnimator is OuterAnimator
    assert NestedShimAnimatorDirect is OuterAnimator
    assert nested_encode_timing_video is encode_timing_video
    assert nested_ffmpeg_cmd is ffmpeg_cmd
    assert nested_ffmpeg_cmd_tpad is ffmpeg_cmd_tpad


def test_schedule_components_handles_empty_and_strictly_fits_draw_duration():
    anim = OuterAnimator.__new__(OuterAnimator)
    assert anim._schedule_components([], draw_duration=3.0) == []

    # 100 tiny components that in the legacy nested implementation would each get
    # min 0.05s (= 5.0s total) and overflow draw_duration=1.5s.
    comps = [{"area": 4, "is_fill": False} for _ in range(100)]
    schedule = anim._schedule_components(comps, draw_duration=1.5, start=0.25)
    assert len(schedule) == 100
    first_start = schedule[0][1]
    last_end = schedule[-1][1] + schedule[-1][2]
    assert abs(first_start - 0.25) < 1e-6
    assert last_end <= 0.25 + 1.5 + 1e-4


def test_render_to_file_and_encode_timing_video_signatures_support_nvenc_and_extra_frames():
    render_sig = inspect.signature(OuterAnimator.render_to_file)
    assert "use_nvenc" in render_sig.parameters
    assert render_sig.parameters["use_nvenc"].default is True
    assert "extra_frames" in render_sig.parameters
    assert render_sig.parameters["extra_frames"].default is None

    encode_sig = inspect.signature(encode_timing_video)
    assert "use_nvenc" in encode_sig.parameters
    assert encode_sig.parameters["use_nvenc"].default is True
    assert "extra_frames" in encode_sig.parameters
    assert encode_sig.parameters["extra_frames"].default is None


def test_ffmpeg_commands_toggle_nvenc_and_libx264_cleanly():
    cmd_nvenc = ffmpeg_cmd(1280, 720, 24, "1500k", "veryfast", "out.mp4", use_nvenc=True)
    assert "h264_nvenc" in cmd_nvenc
    assert "libx264" not in cmd_nvenc

    cmd_cpu = ffmpeg_cmd(1280, 720, 24, "1500k", "veryfast", "out.mp4", use_nvenc=False)
    assert "libx264" in cmd_cpu
    assert "h264_nvenc" not in cmd_cpu

    tpad_nvenc = ffmpeg_cmd_tpad(1280, 720, 24, "1500k", "veryfast", "out.mp4", 1.5, use_nvenc=True)
    assert "h264_nvenc" in tpad_nvenc
    assert any(arg.startswith("tpad=stop_mode=clone:stop=36") for arg in tpad_nvenc)


def test_encode_timing_video_streams_extra_frames_and_disables_tpad():
    img = np.full((16, 16, 3), 255, dtype=np.uint8)
    img[4:8, 4:8] = [10, 20, 30]
    timing = np.full((16, 16), np.inf, dtype=np.float32)
    timing[4:8, 4:8] = 0.05

    extra = [
        np.full((8, 8, 3), 100, dtype=np.uint8),  # intentionally smaller to test auto-resize to 16x16
        np.full((16, 16, 3), 150, dtype=np.uint8),
    ]

    captured_cmds = []
    written_chunks = []

    class FakePipe:
        def write(self, data):
            written_chunks.append(bytes(data))

    @contextmanager
    def fake_ffmpeg_pipe(cmd):
        captured_cmds.append(cmd)
        yield FakePipe()

    with patch("backend.whiteboard_animator.video_encoding._ffmpeg_pipe", side_effect=fake_ffmpeg_pipe):
        encode_timing_video(
            img_array=img,
            timing=timing,
            draw_end=0.1,
            total_duration=2.0,
            fade_dur=0.05,
            output_path="dummy.mp4",
            fps=10,
            bitrate="1500k",
            preset="veryfast",
            use_nvenc=True,
            extra_frames=extra,
        )

    assert len(captured_cmds) == 1
    cmd = captured_cmds[0]
    assert "h264_nvenc" in cmd
    # Because extra_frames are provided, tpad must not be used even though hold_dur > 0.5
    assert not any("tpad=" in str(arg) for arg in cmd)

    draw_frames = int((0.1 + 0.05) * 10) + 1
    assert len(written_chunks) == draw_frames + len(extra)
    assert all(len(chunk) == 16 * 16 * 3 for chunk in written_chunks)
