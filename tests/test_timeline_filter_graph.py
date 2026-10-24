"""Unit test cho filter-graph builder timeline CapCut (P2: speed/volume/crop/fade/reverse/freeze/detach)."""
import math
import re
import sys
import pathlib

BACKEND_DIR = pathlib.Path(__file__).resolve().parents[1] / "backend"
sys.path.insert(0, str(BACKEND_DIR))

from workflow_api import (  # noqa: E402
    _atempo_chain,
    _build_timeline_filter_graph,
    _retime_subtitles_to_timeline,
    _timeline_clip_effects,
)


def _clip(start, end, **kw):
    c = {"start": start, "end": end}
    c.update(kw)
    return c


def _joined(graph):
    return ";".join(graph["filter_lines"])


# ---------------------------------------------------------------- atempo chain
def test_atempo_chain_identity_and_extremes():
    assert _atempo_chain(1.0) == []
    assert _atempo_chain(4.0) == ["atempo=2.0", "atempo=2.00000"]
    assert _atempo_chain(0.25) == ["atempo=0.5", "atempo=0.50000"]


def test_atempo_chain_product_matches_speed():
    for speed in (0.25, 0.5, 1.25, 1.5, 2.0, 3.0, 3.7, 4.0):
        product = 1.0
        for part in _atempo_chain(speed):
            product *= float(part.split("=")[1])
        assert math.isclose(product, speed, rel_tol=1e-4), speed


# ---------------------------------------------------------------- speed
def test_speed_two_halves_duration_and_uses_setpts():
    g = _build_timeline_filter_graph([_clip(0, 10, speed=2)], has_audio=False)
    assert g["total_dur"] == 5.0
    joined = _joined(g)
    assert "setpts=(PTS-STARTPTS)/2.00000" in joined
    # setpts (thời gian) đứng trước, format=yuv420p mở đầu phần xử lý pixel
    chain = [ln for ln in g["filter_lines"] if ln.startswith("[0:v]")][0]
    assert chain.index("setpts") < chain.index("format=yuv420p")


def test_speed_keep_pitch_uses_atempo_and_no_pitch_uses_asetpts():
    g = _build_timeline_filter_graph([_clip(0, 10, speed=2, keepPitch=True)], has_audio=True)
    assert "atempo=2.00000" in _joined(g)
    g2 = _build_timeline_filter_graph([_clip(0, 10, speed=2, keepPitch=False)], has_audio=True)
    j2 = _joined(g2)
    assert "asetpts=(PTS-STARTPTS)/2.00000" in j2
    assert "atempo" not in j2


# ---------------------------------------------------------------- volume / mute
def test_volume_and_mute():
    g = _build_timeline_filter_graph([_clip(0, 5, volume=0.5)], has_audio=True)
    assert "volume=0.50000" in _joined(g)
    g2 = _build_timeline_filter_graph([_clip(0, 5, muted=True)], has_audio=True)
    assert "volume=0" in _joined(g2)
    # muted vẫn giữ nhánh audio để concat đều
    assert len(g2["a_parts"]) == 1


# ---------------------------------------------------------------- fade
def test_fade_out_position_uses_timeline_duration():
    g = _build_timeline_filter_graph([_clip(0, 10, speed=2, fadeOut=1)], has_audio=True)
    joined = _joined(g)
    # tl_dur = 5s → fade out bắt đầu ở 4s
    assert "fade=t=out:st=4.000:d=1.000" in joined
    assert "afade=t=out:st=4.000:d=1.000" in joined


# ---------------------------------------------------------------- crop / rotate
def test_crop_uses_yuv420p_first_then_refit_to_source():
    g = _build_timeline_filter_graph(
        [_clip(0, 5, crop={"x": 0.1, "y": 0.1, "w": 0.5, "h": 0.5})],
        has_audio=False, src_w=1080, src_h=1920,
    )
    chain = [ln for ln in g["filter_lines"] if ln.startswith("[0:v]")][0]
    assert chain.index("format=yuv420p") < chain.index("crop=")
    assert "scale=1080:1920:force_original_aspect_ratio=decrease" in chain
    assert "pad=1080:1920" in chain
    assert "trunc(iw*0.50000/2)*2" in chain


def test_rotate_transpose_and_refit():
    g90 = _build_timeline_filter_graph([_clip(0, 5, rotate=90)], has_audio=False, src_w=1080, src_h=1920)
    chain90 = [ln for ln in g90["filter_lines"] if ln.startswith("[0:v]")][0]
    assert "transpose=1" in chain90 and "pad=1080:1920" in chain90
    g180 = _build_timeline_filter_graph([_clip(0, 5, rotate=180)], has_audio=False)
    assert "transpose=1,transpose=1" in _joined(g180)
    g270 = _build_timeline_filter_graph([_clip(0, 5, rotate=270)], has_audio=False)
    assert "transpose=2" in _joined(g270)
    # rotate 180 không đổi kích thước → không cần refit
    chain180 = [ln for ln in g180["filter_lines"] if ln.startswith("[0:v]")][0]
    assert "pad=" not in chain180


def test_flip():
    g = _build_timeline_filter_graph([_clip(0, 5, flipH=True, flipV=True)], has_audio=False)
    joined = _joined(g)
    assert "hflip" in joined and "vflip" in joined


# ---------------------------------------------------------------- reverse
def test_reverse_allowed_short_and_dropped_long():
    g = _build_timeline_filter_graph([_clip(0, 10, reverse=True)], has_audio=True)
    joined = _joined(g)
    assert ",reverse" in joined and "areverse" in joined
    g2 = _build_timeline_filter_graph([_clip(0, 70, reverse=True)], has_audio=True)
    j2 = _joined(g2)
    assert ",reverse" not in j2 and "areverse" not in j2


# ---------------------------------------------------------------- freeze
def test_freeze_pads_video_and_delays_audio():
    g = _build_timeline_filter_graph([_clip(0, 10, freeze=2)], has_audio=True)
    assert g["total_dur"] == 12.0
    joined = _joined(g)
    assert "tpad=start_duration=2.000:start_mode=add" in joined
    assert "adelay=2000:all=1" in joined


# ---------------------------------------------------------------- detach audio
def test_detach_audio_switches_to_mix_mode():
    g = _build_timeline_filter_graph(
        [_clip(0, 10, speed=2), _clip(10, 20, detachAudio=True)],
        has_audio=True,
    )
    assert g["mode"] == "mix"
    joined = _joined(g)
    assert "concat=n=2:v=1:a=0[outv]" in joined
    assert "adelay=0:all=1" in joined          # clip 1 bắt đầu ở giây 0
    assert "adelay=5000:all=1" in joined       # clip 2 bắt đầu ở giây 5 (clip 1 chạy 2x)
    assert "amix=inputs=2:normalize=0" in joined
    assert "apad=whole_dur=15.000" in joined
    # audio clip detach (a1) giữ tốc độ gốc: không atempo/asetpts chia speed
    assert "[0:a]atrim=start=10.000:end=20.000,asetpts=PTS-STARTPTS[a1]" in joined


# ---------------------------------------------------------------- concat mode giữ hành vi cũ
def test_plain_clips_still_use_proven_concat_mode():
    g = _build_timeline_filter_graph([_clip(0, 5), _clip(10, 15)], has_audio=True)
    assert g["mode"] == "concat"
    assert "concat=n=2:v=1:a=1[outv][outa]" in _joined(g)


# ---------------------------------------------------------------- re-time sub theo speed/freeze
def test_retime_subtitles_with_speed():
    subs = [{"start": 4.0, "end": 6.0, "text": "xin chao"}]
    out = _retime_subtitles_to_timeline(subs, [], [_clip(0, 10, speed=2)])
    assert len(out) == 1
    assert out[0]["start"] == 2.0 and out[0]["end"] == 3.0
    assert out[0]["text"] == "xin chao"


def test_retime_subtitles_with_freeze_offset():
    subs = [{"start": 0.0, "end": 2.0, "text": "a"}]
    out = _retime_subtitles_to_timeline(subs, [], [_clip(0, 10, freeze=1)])
    assert out[0]["start"] == 1.0 and out[0]["end"] == 3.0


def test_retime_subtitles_skips_cut_region():
    # sub 8..12 bị cắt một phần khi clip chỉ giữ 0..10 rồi 20..30
    subs = [{"start": 8.0, "end": 12.0, "text": "x"}, {"start": 22.0, "end": 24.0, "text": "y"}]
    out = _retime_subtitles_to_timeline(subs, [], [_clip(0, 10), _clip(20, 30)])
    assert len(out) == 2
    # phần 8..10 map sang 8..10; phần 22..24 map sang 12..14
    assert out[0]["end"] == 10.0
    assert out[1]["start"] == 12.0 and out[1]["end"] == 14.0


# ---------------------------------------------------------------- clamp biên
def test_effects_clamped():
    eff = _timeline_clip_effects(_clip(0, 1, speed=99, volume=-5, rotate=137, freeze=999))
    assert eff["speed"] == 4.0
    assert eff["volume"] == 0.0
    assert eff["rotate"] == 90
    assert eff["freeze"] == 10.0
