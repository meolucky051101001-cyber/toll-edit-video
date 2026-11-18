"""Pure geometry/filter/ASS tests; no model, provider or GPU execution."""
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import patch
import json
import pytest

from backend import subtitle_blur as blur
from backend.ass_utils import generate_ass_file
from backend.v1_ocr_geometry import OCR_GEOMETRY_VERSION


def box(start=.4, end=1., x=.2, right=.7, y=.8, bottom=.85):
    return dict(start=start, end=end, x_pct=x, max_x_pct=right,
                y_pct=y, max_y_pct=bottom, geometry_verified=True)


def segment(tracks=None, **fields):
    tracks = [box()] if tracks is None else tracks
    return NS(index=1, content="Phụ đề tiếng Việt", start=timedelta(),
              end=timedelta(seconds=4), tracking_blocks=tracks,
              best_block=tracks[0] if tracks else None, geometry_verified=True,
              ocr_mask_status="located", **fields)


def test_ass_blur_has_only_crisp_text_and_source_metadata(tmp_path):
    seg = segment([box(), box(1.8, 2.2)])
    output = tmp_path / "blur.ass"
    generate_ass_file([seg], [], output, cover_style="localized_blur")
    text = output.read_text(encoding="utf-8-sig")
    assert "Dialogue: 0," not in text
    assert "Dialogue: 1," in text
    assert ",1,2,1,5,10,10,10,1" in text  # outline and shadow, no background box
    metadata = blur.read_ass_blur_metadata(text)
    assert [(b["start"], b["end"]) for b in metadata["boxes"]] == [(.4, 1), (1.8, 2.2)]
    assert seg.end.total_seconds() == 4  # speech duration never supplies blur lifetime


def test_verified_empty_never_reuses_best_box():
    seg = segment([])
    seg.best_block = box(0, 4)
    assert blur.collect_source_blur_boxes([seg]) == []


def test_vietnamese_ink_expands_blur_without_extending_source_lifetime(tmp_path):
    source = box(.4, 1., x=.46, right=.57, y=.8, bottom=.83)
    seg = segment([source])
    seg.content = 'Những món phụ kiện đóng gói xinh xắn dành cho mọi người'
    output = tmp_path / 'measured.ass'
    generate_ass_file([seg], [], output, play_res_x=720, play_res_y=1280,
                      cover_style='localized_blur')
    metadata = blur.read_ass_blur_metadata(output.read_text(encoding='utf-8-sig'))
    assert metadata['boxes'] == blur.validate_blur_boxes([source])
    text_boxes = metadata['text_boxes']
    assert max(b['max_x_pct'] - b['x_pct'] for b in text_boxes) > .5
    assert all(b['max_y_pct'] - b['y_pct'] < .15 for b in text_boxes)
    assert any(b['end'] > 1 for b in text_boxes)
    assert max(b['end'] for b in metadata['boxes']) == 1


def test_combined_blur_unions_only_concurrent_intersecting_regions():
    source = box(0, 1, x=.4, right=.6, y=.7, bottom=.75)
    translated = box(.5, 2, x=.2, right=.8, y=.72, bottom=.8)
    distant = box(.5, 2, x=.05, right=.15, y=.1, bottom=.15)
    result = blur.combine_blur_boxes([source], [translated, distant])
    initial = [b for b in result if b['start'] == 0]
    assert len(initial) == 1 and initial[0]['x_pct'] == .4
    concurrent = [b for b in result if b['start'] == .5]
    assert len(concurrent) == 2
    joined = next(b for b in concurrent if b['y_pct'] == .7)
    assert (joined['x_pct'], joined['max_x_pct'], joined['max_y_pct']) == (.2,.8,.8)
    after = [b for b in result if b['start'] == 1]
    assert all(b['y_pct'] != .7 for b in after) and len(after) == 2


@pytest.mark.parametrize('width,height', [(360,640),(640,360)])
def test_measured_text_blur_contains_real_libass_glyph_pixels(tmp_path,width,height):
    import math, shutil, subprocess
    import numpy as np
    ffmpeg = shutil.which('ffmpeg')
    if not ffmpeg:
        pytest.skip('FFmpeg required')
    seg = segment([box(0,2,x=.46,right=.57,y=.72,bottom=.76)])
    seg.end = timedelta(seconds=2)
    seg.content = 'Những phụ kiện xinh xắn của Nguyễn giúp đóng gói quà thật đẹp và tiện lợi'
    output = tmp_path / 'glyphs.ass'
    generate_ass_file([seg],[],output,play_res_x=width,play_res_y=height,cover_style='localized_blur')
    metadata = blur.read_ass_blur_metadata(output.read_text(encoding='utf-8-sig'))
    escaped = str(output).replace('\\','/').replace(':','\\:')
    result = subprocess.run([ffmpeg,'-v','error','-nostdin','-threads','1','-filter_threads','1',
        '-f','lavfi','-i',f'color=black:s={width}x{height}:r=1','-vf',f"ass='{escaped}'",
        '-frames:v','1','-pix_fmt','gray','-threads','1','-f','rawvideo','-'],capture_output=True,timeout=20)
    assert result.returncode == 0, result.stderr.decode(errors='replace')
    pixels = np.frombuffer(result.stdout,dtype=np.uint8).reshape(height,width)
    mask = np.zeros((height,width),dtype=bool)
    for b in metadata['text_boxes']:
        if b['start'] <= 0 < b['end']:
            mask[math.floor(b['y_pct']*height):math.ceil(b['max_y_pct']*height),
                 math.floor(b['x_pct']*width):math.ceil(b['max_x_pct']*width)] = True
    assert np.any(pixels > 1)
    assert not np.any((pixels > 1) & ~mask)
    assert mask.sum() < width*height*.2


def test_tts_fitting_keeps_verified_source_tracking_gaps():
    from backend.ass_utils import sync_and_clamp_subtitles
    seg = segment([box(.4, 1), box(1.8, 2.2)])
    sync_and_clamp_subtitles([seg], [{"index": 1, "duration": 3.8}])
    assert seg.end.total_seconds() == 3.8
    assert [(b["start"], b["end"]) for b in seg.tracking_blocks] == [(.4, 1), (1.8, 2.2)]


def test_ass_can_use_original_ocr_timing_when_old_tts_tracking_is_stretched(tmp_path):
    output = tmp_path / "source.ass"
    generate_ass_file([segment([box(0, 4)])], [], output, cover_style="localized_blur",
                      source_segments=[segment([box(.4, 1)])])
    metadata = blur.read_ass_blur_metadata(output.read_text(encoding="utf-8-sig"))
    assert metadata["boxes"][0]["start"] == .4
    assert metadata["boxes"][0]["end"] == 1


def test_source_geometry_is_required_and_unresolved_is_not_guessed():
    with pytest.raises(ValueError, match="Thiếu observation"):
        blur.collect_source_blur_boxes([NS(index=1, start=0, end=5)])
    seg = segment()
    seg.ocr_mask_status = "unresolved"
    with pytest.raises(ValueError, match="Chưa xác minh"):
        blur.collect_source_blur_boxes([seg])


def test_missing_source_times_never_fall_back_to_fitted_tts():
    b = box()
    del b["start"]
    with pytest.raises(ValueError, match="thời gian nguồn"):
        blur.collect_source_blur_boxes([segment([b])])


def test_blur_preserves_all_gaps_even_with_many_intervals():
    boxes = [box(i * .3, i * .3 + .1) for i in range(25)]
    regions = blur.blur_regions(boxes, 720, 1280)
    assert len(regions) == 1
    assert len(regions[0][-1]) == 25
    assert regions[0][-1][1] == (.3, .4)


def test_moving_regions_never_union_unrelated_screen_areas():
    boxes = [box(i, i + .5, x=.02 + i * .13, right=.1 + i * .13) for i in range(6)]
    regions = blur.blur_regions(boxes, 720, 1280)
    assert len(regions) == 6
    assert all(w < 100 for _, _, w, _, _ in regions)


def test_half_open_intervals_and_safe_pixel_format_precede_subtitles():
    chain = blur.build_localized_blur_filter([box()], 720, 1280)
    assert chain.startswith("format=yuv420p,split")
    assert "gte(t,0.400000)*lt(t,1.000000)" in chain
    assert "boxblur=" in chain
    assert blur.build_localized_blur_filter([], 720, 1280) == ""


def test_fixed_size_slots_share_positions_but_keep_overlapping_order():
    settings = dict(sub_blur_padding_x=0, sub_blur_padding_y=0)
    moving = [box(0, 1, x=.1, right=.3, y=.1, bottom=.3),
              box(2, 3, x=.6, right=.8, y=.6, bottom=.8)]
    graph = blur.build_localized_blur_filter(moving, 100, 100, settings)
    assert 'split=2[' in graph
    assert graph.count('boxblur=') == graph.count('overlay=') == 1
    # Moving region 3 before middle region 2 would reverse their overlay order.
    ordered = [moving[0], box(1, 4, x=.5, right=.9, y=.5, bottom=.9), moving[1]]
    graph = blur.build_localized_blur_filter(ordered, 100, 100, settings)
    assert 'split=4[' in graph
    assert 'select=' not in graph  # avoid framesync buffering whole inactive gaps


def test_renderer_v6_accepts_v5_geometry_without_reinterpreting_boxes():
    metadata = blur.ass_blur_metadata([segment()])
    current = blur.read_ass_blur_metadata(metadata)
    previous = blur.read_ass_blur_metadata(metadata.replace('"version":6', '"version":5', 1))
    assert previous == current
    with pytest.raises(ValueError, match='tạo lại'):
        blur.read_ass_blur_metadata(metadata.replace('"version":6', '"version":4', 1))


@pytest.mark.parametrize("b", [box(x=0, right=.001, y=0, bottom=.001),
                                box(x=.999, right=1, y=.999, bottom=1)])
def test_edge_regions_stay_inside_video(b):
    for x, y, w, h, _ in blur.blur_regions([b], 720, 1280):
        assert 0 <= x < x+w <= 720
        assert 0 <= y < y+h <= 1280
        assert all(v % 2 == 0 for v in (x, y, w, h))


@pytest.mark.parametrize("change", [{"end": float("nan")}, {"max_x_pct": 2}, {"start": -1}])
def test_invalid_boxes_fail_closed(change):
    with pytest.raises(ValueError):
        blur.blur_regions([{**box(), **change}], 720, 1280)


def test_settings_are_bounded_and_frozen_without_live_fallback():
    assert blur.freeze_subtitle_settings({})["sub_cover_style"] == "solid_box"
    frozen = blur.freeze_subtitle_settings({"sub_cover_style": "localized_blur", "sub_blur_radius": 8})
    assert blur.freeze_subtitle_settings(frozen, {"sub_blur_radius": 30})["sub_blur_radius"] == 8
    for invalid in (float("nan"), 33, -1, True):
        with pytest.raises(ValueError):
            blur.resolve_blur_settings({"sub_blur_radius": invalid})


def test_handoff_blur_requires_explicit_source_box():
    from backend.handoff_api import _attach_geometry
    cue = NS(content="Một câu", start=timedelta(), end=timedelta(seconds=1))
    with pytest.raises(ValueError, match="Thiếu tọa độ"):
        _attach_geometry(cue, None, .85, require_source_geometry=True)


def test_workflow_blur_needs_source_artifacts_not_baked_ass(tmp_path):
    from backend.workflow_api import build_custom_ass_file
    (tmp_path / "final.ass").write_text("[Events]\nDialogue: 0,old white box", encoding="utf-8")
    with pytest.raises(ValueError, match="artifact TTS/OCR"):
        build_custom_ass_file(tmp_path, {"sub_cover_style": "localized_blur"})


def test_workflow_blur_never_substitutes_tts_for_missing_ocr_segments(tmp_path):
    from backend.workflow_api import build_custom_ass_file
    from backend.v1_stage_runtime import save_payload
    save_payload(tmp_path / "tts.json", segments=[segment()], dubs=[])
    save_payload(tmp_path / "ocr.json", width=720, height=1280)
    with pytest.raises(ValueError, match="OCR thiếu"):
        build_custom_ass_file(tmp_path, {"sub_cover_style": "localized_blur"})
    assert not (tmp_path / "blur_source.ass").exists()


@pytest.mark.parametrize("version", [None, OCR_GEOMETRY_VERSION - 1])
def test_workflow_rejects_old_geometry_before_generating_blur(tmp_path, version):
    from backend.workflow_api import build_custom_ass_file
    from backend.v1_stage_runtime import save_payload
    save_payload(tmp_path / "tts.json", segments=[segment()], dubs=[])
    save_payload(tmp_path / "ocr.json", segments=[segment()], geometry_version=version)
    with pytest.raises(ValueError, match="Geometry OCR cũ"):
        build_custom_ass_file(tmp_path, {"sub_cover_style": "localized_blur"})
    assert not (tmp_path / "blur_source.ass").exists()


@pytest.mark.parametrize("many_regions", [False, True])
def test_custom_export_blurs_raw_source_before_crop_and_scale(tmp_path, many_regions):
    from backend import workflow_api as wf, v1_media_streams
    from backend.v1_media_streams import MainVideo
    from backend.v1_stage_runtime import save_payload
    job, inputs, output = [tmp_path / name for name in ("vid", "input", "output")]
    for folder in (job, inputs, output):
        folder.mkdir()
    source = inputs / "vid.mp4"
    source.write_bytes(b"source")
    baked = job / "final_vid.mp4"
    baked.write_bytes(b"old white box")
    (job / "mixed.wav").write_bytes(b"dubbed")
    boxes = [box(i * .02, i * .02 + .01, x=.01 + i * .003, right=.08 + i * .003)
             for i in range(80)] if many_regions else [box()]
    save_payload(job / "tts.json", segments=[segment([box(0, 4)])], dubs=[])
    save_payload(job / "ocr.json", segments=[segment(boxes)], width=720, height=1280,
                 geometry_version=OCR_GEOMETRY_VERSION)
    commands, graphs, scripts = [], [], []
    def fake_run(cmd, *args, **kwargs):
        commands.append(cmd)
        if "-filter_complex_script" in cmd:
            script = Path(cmd[cmd.index("-filter_complex_script") + 1])
            scripts.append(script)
            graphs.append(script.read_text(encoding="utf-8"))
        else:
            graphs.append(cmd[cmd.index("-filter_complex") + 1])
        Path(cmd[-1]).write_bytes(b"rendered" * 2000)
        return NS(returncode=0, stdout="", stderr="")
    with patch.object(wf, "resolve_job_dir", return_value=job), \
         patch.object(wf, "resolve_output_dir", return_value=output), \
         patch.object(wf, "INPUT_DIR", inputs), \
         patch.object(wf, "DOWNLOADS_DIR", tmp_path / "downloads"), \
         patch.object(wf, "OUTPUT_DIR", output), \
         patch.object(wf, "_valid_final_video", return_value=True), \
         patch.object(wf, "_find_vocal_files", return_value=(None, None)), \
         patch.object(wf, "probe_stream_durations", return_value={"video": 4., "audio": 4.}), \
         patch.object(wf, "detect_letterbox_crop", return_value={"w": 680, "h": 1200, "x": 20, "y": 40}), \
         patch.object(wf, "stream_file_url", return_value="fixture-url"), \
         patch.object(v1_media_streams, "probe_main_video", return_value=MainVideo(0,720,1280,30,4,1,1,False)), \
         patch.object(wf, "_run_ffmpeg_ctl", side_effect=fake_run):
        result = wf._execute_custom_export({"stem": "vid", "export_srt": False,
            "canvas_settings": {"sub_cover_style": "localized_blur", "mirror": True}})
    assert result["status"] == "ok"
    cmd, graph = commands[0], graphs[0]
    assert cmd[cmd.index("-i") + 1] == str(source)
    assert str(baked) not in cmd
    assert graph.index("boxblur=") < graph.index("crop=680:1200:20:40") < graph.index("hflip,") < graph.index("scale=") < graph.index("ass=")
    assert bool(scripts) == many_regions
    assert all(not script.exists() for script in scripts)
    if not many_regions:
        assert "gte(t,0.400000)*lt(t,1.000000)" in graph
        assert "gte(t,0.000000)*lt(t,4.000000)" not in graph.split('[export_source];')[0]
        assert graph.index('exporttextmain0') > graph.index('hflip')
        assert graph.index('exporttextmain0') < graph.index('ass=')


def test_batch_framing_rejects_unimplemented_blur_conversion_before_launch(tmp_path):
    import asyncio
    from backend import workflow_api as wf
    with patch.object(wf.threading, "Thread") as worker:
        with pytest.raises(wf.HTTPException) as error:
            asyncio.run(wf.api_start_batch_framing({"input_folder": str(tmp_path),
                                                   "sub_cover_style": "localized_blur"}))
    assert error.value.status_code == 409
    worker.assert_not_called()


def test_custom_export_refuses_to_blur_baked_output_without_source(tmp_path):
    from backend import workflow_api as wf
    job = tmp_path / "workspace" / "vid"
    job.mkdir(parents=True)
    (job / "final_vid.mp4").write_bytes(b"old baked white box")
    with patch.object(wf, "resolve_job_dir", return_value=job), \
         patch.object(wf, "INPUT_DIR", tmp_path / "empty_input"), \
         patch.object(wf, "DOWNLOADS_DIR", tmp_path / "empty_downloads"), \
         patch.object(wf, "OUTPUT_DIR", tmp_path / "output"), \
         patch.object(wf, "_valid_final_video", return_value=True), \
         patch.object(wf, "_run_ffmpeg_ctl") as run:
        with pytest.raises(wf.HTTPException) as error:
            wf._execute_custom_export({"stem": "vid", "canvas_settings": {"sub_cover_style": "localized_blur"}})
    assert error.value.status_code == 409
    assert "video nguồn" in error.value.detail
    run.assert_not_called()


def test_renderer_never_guesses_wide_band_without_metadata(tmp_path):
    from backend import video_utils, v1_media_streams
    from backend.v1_media_streams import MainVideo
    source, audio, ass, output = [tmp_path / p for p in ("in.mp4", "mix.wav", "sub.ass", "out.mp4")]
    for file in (source, audio, ass):
        file.write_text("input", encoding="utf-8")
    with patch.object(v1_media_streams, "probe_main_video", return_value=MainVideo(0,720,1280,30,4,1,1,False)), \
         patch.object(video_utils, "run_batch_subprocess") as run:
        assert video_utils.process_video(str(source), str(ass), str(audio), str(output),
                                         subtitle_cover_style="localized_blur") is False
    run.assert_not_called()


def test_renderer_reads_ass_geometry_and_keeps_text_after_blur(tmp_path):
    from backend import video_utils, v1_media_streams
    from backend.v1_media_streams import MainVideo
    source, audio, ass, output = [tmp_path / p for p in ("in.mp4", "mix.wav", "sub.ass", "out.mp4")]
    source.write_bytes(b"input"); audio.write_bytes(b"input")
    generate_ass_file([segment()], [], ass, cover_style="localized_blur")
    commands = []
    def fake_run(cmd, **kwargs):
        commands.append(cmd)
        output.write_bytes(b"x" * 12000)
        return NS(returncode=0, stdout="", stderr="")
    with patch.object(v1_media_streams, "probe_main_video", return_value=MainVideo(0,720,1280,30,4,1,1,False)), \
         patch.object(v1_media_streams, "validate_video_output"), \
         patch.object(video_utils, "run_batch_subprocess", side_effect=fake_run), \
         patch("canvas_settings.get_canvas_settings", return_value={"sub_cover_style": "solid_box"}):
        assert video_utils.process_video(str(source), str(ass), str(audio), str(output))
    graph = commands[0][commands[0].index("-vf") + 1]
    assert graph.index("boxblur=") < graph.index("subtitles=")
    assert "gte(t,0.400000)*lt(t,1.000000)" in graph


@pytest.mark.parametrize("endpoint", ["api_process_video", "api_process_url"])
def test_legacy_api_uses_frozen_style_and_pre_tts_geometry(tmp_path, monkeypatch, endpoint):
    import asyncio
    import importlib
    import main
    import social_downloader
    import canvas_settings
    import job_config_service
    import v1_feature_flags
    import ass_utils
    from ai import v1_auto_voice
    from pydub import AudioSegment
    from unittest.mock import AsyncMock

    source = tmp_path / "source.mp4"
    source.write_bytes(b"fixture")
    seg = segment()
    cfg = {"sub_cover_style": "localized_blur", "sub_blur_radius": 8}
    monkeypatch.setattr(main, "WORKSPACE", tmp_path / "work")
    monkeypatch.setattr(main, "OUTPUT_DIR", tmp_path / "output")
    monkeypatch.setattr(main, "_validate_input_path", lambda _: source)
    monkeypatch.setattr(main, "run_api_pipeline_v2", AsyncMock(return_value=None))
    monkeypatch.setattr(v1_feature_flags, "get_feature_flags", lambda *_: {"V1_USE_ORCHESTRATOR": False})
    monkeypatch.setattr(job_config_service, "get_frozen_config", lambda *a, **k: {"effective_config": cfg})
    monkeypatch.setattr(canvas_settings, "get_canvas_settings", lambda: {"sub_cover_style": "solid_box", "sub_blur_radius": 30})
    monkeypatch.setattr(main, "extract_audio_from_video", lambda *a: None)
    monkeypatch.setattr(main, "separate_vocals_demucs", lambda *a, **k: ("vocals", "music"))
    monkeypatch.setattr(main, "extract_subtitles_isolated", lambda *a: [seg])
    monkeypatch.setattr(main, "_audio_duration_seconds", lambda *_: 4)
    monkeypatch.setattr(main, "_apply_visual_sub_to_speech", AsyncMock(return_value=([seg], (720,1280,.8), False)))

    def translate(segments, *args, **kwargs):
        # Some legacy adapters mutate the input; source observations must survive.
        segments[0].tracking_blocks[0]["end"] = 4
        return segments

    monkeypatch.setattr(main, "translate_subtitles", translate)
    monkeypatch.setattr(main, "save_srt", lambda *a: None)
    monkeypatch.setattr(v1_auto_voice, "get_auto_voice_mode", lambda *_: "manual")
    monkeypatch.setattr(v1_auto_voice, "decide_video_voice", lambda **k: {"voice_source": "fixture", "voice_param": "fixture"})
    monkeypatch.setattr(AudioSegment, "from_file", lambda *_: b"0" * 4000)
    monkeypatch.setattr(main, "generate_dubbing_audio_isolated", AsyncMock(return_value=[]))
    monkeypatch.setattr(ass_utils, "sync_and_clamp_subtitles", lambda segments, _: segments)
    monkeypatch.setattr(main, "mix_audio_pydub", lambda *a, **k: None)
    renders = []

    def render(video, ass, audio, output, **kwargs):
        metadata = blur.read_ass_blur_metadata(Path(ass).read_text(encoding="utf-8-sig"))
        assert metadata["settings"]["sub_blur_radius"] == 8
        assert metadata["boxes"][0]["end"] == 1
        assert kwargs["subtitle_cover_style"] == "localized_blur"
        assert kwargs["subtitle_blur_settings"]["sub_blur_radius"] == 8
        Path(output).write_bytes(b"rendered")
        renders.append(output)
        return True

    monkeypatch.setattr(main, "process_video", render)
    real_reload = importlib.reload
    monkeypatch.setattr(importlib, "reload", lambda m: m if m is social_downloader else real_reload(m))
    monkeypatch.setattr(social_downloader, "download_social_video", lambda *a: (True, str(source), "fixture", ""))

    async def run():
        monkeypatch.setattr(main, "API_PROCESS_LOCK", asyncio.Lock())
        monkeypatch.setattr(main, "UNIFIED_PIPELINE_LOCK", asyncio.Lock())
        args = dict(target_lang="vi", voice_source="fixture", voice_param="fixture", api_key="",
                    font_name="Arial", font_color="&H00000000", font_weight=2, video_mode="auto")
        args["video_path" if endpoint == "api_process_video" else "url"] = str(source)
        return await getattr(main, endpoint).__wrapped__(**args)

    result = asyncio.run(run())
    assert result["status"] == "success", result
    assert len(renders) == 1
