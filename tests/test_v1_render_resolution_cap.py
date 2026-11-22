"""V1_RENDER_MAX_SHORT_SIDE cap tests; no model, provider or GPU execution."""
import sys
from types import SimpleNamespace as NS
from unittest.mock import Mock, patch
import pytest


def box(start=.4, end=1., x=.2, right=.7, y=.8, bottom=.85):
    return dict(start=start, end=end, x_pct=x, max_x_pct=right,
                y_pct=y, max_y_pct=bottom, geometry_verified=True)


def _run_render(tmp_path, width, height, cap="1080", gpu_result=(True, "ok")):
    from backend import video_utils, v1_media_streams
    from backend.v1_media_streams import MainVideo

    source, audio, subtitles, output = [
        tmp_path / name for name in ("in.mp4", "mix.wav", "sub.srt", "out.mp4")
    ]
    source.write_bytes(b"x" * 21000)
    audio.write_bytes(b"audio")
    subtitles.write_text("subtitle", encoding="utf-8")
    render_gpu = Mock(return_value=gpu_result)
    gpu_module = NS(
        gpu_localized_blur_available=lambda: True,
        render_video_with_gpu_localized_blur=render_gpu,
    )
    commands = []

    def fake_ffmpeg(cmd, **kwargs):
        commands.append(cmd)
        output.write_bytes(b"x" * 12000)
        return NS(returncode=0, stdout="", stderr="")

    with patch.object(v1_media_streams, "probe_main_video",
                      return_value=MainVideo(0, width, height, 30, 4, 1, 1, False)), \
         patch.object(v1_media_streams, "validate_video_output"), \
         patch.object(video_utils, "run_batch_subprocess", side_effect=fake_ffmpeg), \
         patch.dict(sys.modules, {"v1_gpu_subtitle_blur": gpu_module}), \
         patch.dict("os.environ", {"V1_RENDER_MAX_SHORT_SIDE": cap}):
        ok = video_utils.process_video(
            str(source), str(subtitles), str(audio), str(output),
            delogo=False, subtitle_cover_style="localized_blur",
            localized_blur_boxes=[box(0, 1), box(2, 3, x=.7, right=.9)],
        )
    return ok, render_gpu, commands


def _gpu_kwargs(render_gpu):
    assert render_gpu.call_count
    return render_gpu.call_args_list[0].kwargs


def test_4k_vertical_is_capped_to_1080p(tmp_path):
    ok, render_gpu, _ = _run_render(tmp_path, 2160, 3840)
    assert ok
    kwargs = _gpu_kwargs(render_gpu)
    assert kwargs["pre_blur_vf"].split(",")[0] == "scale=1080:1920"
    assert (kwargs["blur_width"], kwargs["blur_height"]) == (1080, 1920)
    assert (kwargs["output_width"], kwargs["output_height"]) == (1080, 1920)


def test_cpu_filtergraph_gets_the_same_scale(tmp_path):
    ok, _, commands = _run_render(tmp_path, 2160, 3840, gpu_result=(False, "unsupported"))
    assert ok
    graph = commands[0][commands[0].index("-vf") + 1]
    assert graph.split(",")[0] == "scale=1080:1920"


def test_1080p_source_is_not_rescaled(tmp_path):
    ok, render_gpu, _ = _run_render(tmp_path, 1080, 1920)
    assert ok
    kwargs = _gpu_kwargs(render_gpu)
    assert "scale=" not in kwargs["pre_blur_vf"]
    assert (kwargs["blur_width"], kwargs["blur_height"]) == (1080, 1920)
    assert (kwargs["output_width"], kwargs["output_height"]) == (1080, 1920)


def test_720p_source_is_never_upscaled(tmp_path):
    ok, render_gpu, _ = _run_render(tmp_path, 720, 1280)
    assert ok
    kwargs = _gpu_kwargs(render_gpu)
    assert "scale=" not in kwargs["pre_blur_vf"]
    assert (kwargs["blur_width"], kwargs["blur_height"]) == (720, 1280)
    assert (kwargs["output_width"], kwargs["output_height"]) == (720, 1280)


def test_disabled_cap_keeps_native_4k(tmp_path):
    ok, render_gpu, _ = _run_render(tmp_path, 2160, 3840, cap="0")
    assert ok
    kwargs = _gpu_kwargs(render_gpu)
    assert "scale=" not in kwargs["pre_blur_vf"]
    assert (kwargs["blur_width"], kwargs["blur_height"]) == (2160, 3840)
    assert (kwargs["output_width"], kwargs["output_height"]) == (2160, 3840)


def test_nvenc_bounds_still_apply_when_cap_disabled(tmp_path):
    ok, render_gpu, _ = _run_render(tmp_path, 4320, 7680, cap="0")
    assert ok
    kwargs = _gpu_kwargs(render_gpu)
    assert kwargs["pre_blur_vf"].split(",")[0] == "scale=1080:1920"
    assert (kwargs["blur_width"], kwargs["blur_height"]) == (1080, 1920)
    assert (kwargs["output_width"], kwargs["output_height"]) == (1080, 1920)


def test_invalid_cap_value_falls_back_to_default(tmp_path):
    ok, render_gpu, _ = _run_render(tmp_path, 2160, 3840, cap="abc")
    assert ok
    kwargs = _gpu_kwargs(render_gpu)
    assert kwargs["pre_blur_vf"].split(",")[0] == "scale=1080:1920"
