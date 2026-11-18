"""Regression tests for localized blur stability on floating/occluded XHS captions."""
from datetime import timedelta
from types import SimpleNamespace as NS
from unittest.mock import patch

import cv2
import numpy as np
import pytest

from backend import ocr_utils as ocr
from backend import subtitle_blur as blur
from backend.ass_utils import generate_ass_file


def _trk(text, start, end, left, right, top=0.258, bottom=0.298, prob=0.98):
    return NS(
        text=text,
        start=start,
        end=end,
        x_pct=left,
        max_x_pct=right,
        y_pct=top,
        max_y_pct=bottom,
        prob=prob,
        is_subtitle=True,
        in_subtitle_band=True,
        is_packaging=False,
        is_static=False,
        geometry_verified=True,
        visual_locked=False,
    )


def test_partial_occlusion_fragments_coalesce_into_single_envelope():
    tracks = [
        _trk("期主包购入的muji笔盒分", 0.267, 0.333, 0.096, 0.543, prob=0.98),
        _trk("的muji笔盒分", 0.333, 0.600, 0.269, 0.542, prob=0.99),
        _trk("期主包购入的muji笔盒分", 0.600, 1.000, 0.096, 0.548, prob=0.99),
        _trk("入的muji笔盒分", 1.000, 1.167, 0.266, 0.532, prob=0.98),
        _trk("期主包购入的muji笔盒分", 1.167, 1.200, 0.097, 0.542, prob=0.98),
        _trk("期主包购入的muji笔盒分享呀", 1.200, 2.300, 0.097, 0.649, prob=0.985),
    ]
    merged = ocr._coalesce_stationary_source_tracks(tracks, max_chunk_s=None)
    assert len(merged) == 1
    assert merged[0].start == pytest.approx(0.267)
    assert merged[0].end == pytest.approx(2.300)
    assert merged[0].x_pct == pytest.approx(0.096)
    assert merged[0].max_x_pct == pytest.approx(0.649)
    assert merged[0].text == "期主包购入的muji笔盒分享呀"


def test_suffix_occlusion_fragment_merges_without_merging_distinct_captions():
    tracks = [
        _trk("主包日常上学感觉够用", 8.933, 9.900, 0.168, 0.577, top=0.250, bottom=0.288),
        _trk("觉够用了", 9.900, 9.967, 0.430, 0.602, top=0.250, bottom=0.287),
        _trk("主包日常上学感觉够用了", 9.967, 10.667, 0.168, 0.606, top=0.248, bottom=0.288),
    ]
    merged = ocr._coalesce_stationary_source_tracks(tracks, max_chunk_s=None)
    assert len(merged) == 1
    assert merged[0].x_pct == pytest.approx(0.168)
    assert merged[0].max_x_pct == pytest.approx(0.606)
    assert merged[0].text == "主包日常上学感觉够用了"


def test_attach_source_tracks_ignores_tiny_boundary_graze_and_expands_leading_yi():
    seg1 = NS(
        index=1,
        start=timedelta(seconds=0.32),
        end=timedelta(seconds=2.22),
        content="一期主包购入的母鸡笔盒分享呀",
        paraformer_alt="",
    )
    seg7 = NS(
        index=7,
        start=timedelta(seconds=15.38),
        end=timedelta(seconds=16.38),
        content="非常的硬式疯呀",
        paraformer_alt="",
    )
    seg8 = NS(
        index=8,
        start=timedelta(seconds=16.46),
        end=timedelta(seconds=18.00),
        content="然后是这个最小号的笔盒呀",
        paraformer_alt="",
    )
    tracks = [
        _trk("期主包购入的muji笔盒分享呀", 0.267, 2.300, 0.097, 0.649, top=0.258, bottom=0.298),
        _trk("非常的硬史风呀", 15.267, 16.467, 0.165, 0.457, top=0.345, bottom=0.382),
        _trk("然后是这个最小号的笔盒呀", 16.500, 18.067, 0.259, 0.761, top=0.192, bottom=0.225),
    ]
    ocr._attach_source_caption_tracks([seg1, seg7, seg8], tracks)

    # Leading thin '一' stroke compensated on seg1
    assert len(seg1.tracking_blocks) == 1
    assert seg1.tracking_blocks[0].x_pct < 0.080

    # 7ms overlap at 16.460..16.467s stays on seg7 and never pollutes seg8
    assert len(seg7.tracking_blocks) == 1
    assert (seg7.tracking_blocks[0].start, seg7.tracking_blocks[0].end) == pytest.approx((15.267, 16.467))
    assert len(seg8.tracking_blocks) == 1
    assert (seg8.tracking_blocks[0].start, seg8.tracking_blocks[0].end) == pytest.approx((16.500, 18.067))


def test_compound_asr_clause_at_secondary_y_band_is_not_dropped_from_source_timeline():
    pts = [i / 10.0 for i in range(20)]

    class Capture:
        index = 0
        def set(self, prop, value):
            self.index = 0
            return True
        def read(self):
            if self.index >= len(pts):
                return False, None
            img = np.full((640, 360, 3), self.index, dtype=np.uint8)
            self.index += 1
            return True, img
        def get(self, prop):
            return {
                cv2.CAP_PROP_FRAME_COUNT: len(pts),
                cv2.CAP_PROP_POS_FRAMES: self.index,
                cv2.CAP_PROP_POS_MSEC: pts[max(0, self.index - 1)] * 1000.0,
            }.get(prop, 0)

    def recognize(images):
        out = []
        for frame in images:
            idx = int(frame[0, 0, 0])
            if idx < 14:
                # Main band at Y ~ 0.20 (128..148 of 640)
                out.append([([[140, 125], [260, 125], [260, 148], [140, 148]], "可以立在桌上", 0.99)])
            else:
                # Floating left-aligned clause at Y ~ 0.31 (192..215 of 640)
                out.append([([[50, 192], [170, 192], [170, 215], [50, 215]], "大一点的文具也能放", 0.99)])
        return out

    asr_seg = NS(
        index=6,
        start=timedelta(seconds=0.0),
        end=timedelta(seconds=2.0),
        content="但是当笔筒还挺好用的可以立在桌上大一点的文具也能放",
    )
    band = NS(top=0.18, bottom=0.24, support=8, mode="asr_match", selected_by_segment={})
    with patch.object(ocr, "_readtext_batch", side_effect=recognize), \
         patch.object(ocr, "detect_static_watermarks", return_value=[]):
        tracks, _ = ocr._perform_visual_subtitle_ocr(
            Capture(), 360, 640, 2.0, [], lambda: False,
            source_band=band, source_segments=[asr_seg],
        )
    assert any("大一点的文具也能放" in t.text for t in tracks)
    assert all(not t.visual_locked for t in tracks)


def test_localized_blur_disjoint_band_cue_bridges_gap_without_dual_blur_bars(tmp_path):
    from backend.ass_utils import sync_and_clamp_subtitles

    t1 = _trk("主包日常上学感觉够用了", 8.933, 10.667, 0.168, 0.606, top=0.248, bottom=0.288)
    t2 = _trk("这个是眼镜盒但是当笔筒还挺好用的", 10.833, 11.640, 0.257, 0.889, top=0.190, bottom=0.228)
    seg5 = NS(
        index=5,
        start=timedelta(seconds=8.96),
        end=timedelta(seconds=11.64),
        content="Dùng đi học hàng ngày thấy rất ổn, đây là hộp đựng kính",
        tracking_blocks=[t1, t2],
        best_block=t1,
        ocr_mask_status="located",
        geometry_verified=True,
        visual_sub_to_speech=False,
    )
    seg6 = NS(
        index=6,
        start=timedelta(seconds=11.66),
        end=timedelta(seconds=15.14),
        content="Cắm bút rất tiện",
        tracking_blocks=[],
        best_block=None,
        ocr_mask_status="no_caption_evidence",
        geometry_verified=True,
        visual_sub_to_speech=False,
    )
    # Even when condensed TTS finishes speaking early (1.73s -> 10.69s), seg5.end holds
    # over its active source caption window up to 11.64s (<= seg6.start=11.66s).
    sync_and_clamp_subtitles([seg5, seg6], [{"index": 5, "duration": 1.73}, {"index": 6, "duration": 2.5}])
    assert seg5.end.total_seconds() == pytest.approx(11.64)

    ass_path = tmp_path / "stable_disjoint.ass"
    generate_ass_file([seg5], [], ass_path, play_res_x=720, play_res_y=1280, cover_style="localized_blur")
    ass_text = ass_path.read_text(encoding="utf-8-sig")
    dialogues = [ln for ln in ass_text.splitlines() if ln.startswith("Dialogue: 1,")]
    assert len(dialogues) == 2
    assert "0:00:08.96,0:00:10.83" in dialogues[0]
    assert "0:00:10.83,0:00:11.64" in dialogues[1]

    meta = blur.read_ass_blur_metadata(ass_text)
    combined = blur.combine_blur_boxes(meta["boxes"], meta["text_boxes"])
    regions = blur.blur_regions(combined, 720, 1280, meta["settings"])
    # Exactly 1 lower region [8.933 -> 10.833] and 1 upper region [10.833 -> 11.640]:
    # zero missing-sub gap and never 2 simultaneous blur bars at t=11.0s!
    assert len(regions) == 2
    active_at_11 = [
        (x, y, w, h)
        for x, y, w, h, ivs in regions
        for s, e in ivs
        if s <= 11.0 < e
    ]
    assert len(active_at_11) == 1


def test_same_band_multi_track_cue_stays_single_motionless_blur_region(tmp_path):
    from backend.ass_utils import sync_and_clamp_subtitles

    s1 = _trk("这个笔筒可以给到一个夯", 20.833, 22.233, 0.314, 0.759, top=0.324, bottom=0.364)
    s2 = _trk("肥肠平价还可以放下主包所有的笔", 22.233, 24.200, 0.204, 0.799, top=0.311, bottom=0.349)
    seg10 = NS(
        index=10,
        start=timedelta(seconds=20.78),
        end=timedelta(seconds=24.12),
        content="Ống bút này điểm mười, giá rẻ lại đựng vừa hết bút",
        tracking_blocks=[s1, s2],
        best_block=s2,
        ocr_mask_status="located",
        geometry_verified=True,
        visual_sub_to_speech=False,
    )
    seg11 = NS(
        index=11,
        start=timedelta(seconds=24.20),
        end=timedelta(seconds=25.32),
        content="Hẹn gặp lại",
        tracking_blocks=[],
        best_block=None,
        ocr_mask_status="no_caption_evidence",
        geometry_verified=True,
        visual_sub_to_speech=False,
    )
    sync_and_clamp_subtitles([seg10, seg11], [{"index": 10, "duration": 1.99}, {"index": 11, "duration": 0.94}])
    assert seg10.end.total_seconds() == pytest.approx(24.20)

    ass_path = tmp_path / "stable_same_band.ass"
    generate_ass_file([seg10], [], ass_path, play_res_x=720, play_res_y=1280, cover_style="localized_blur")
    ass_text = ass_path.read_text(encoding="utf-8-sig")
    dialogues = [ln for ln in ass_text.splitlines() if ln.startswith("Dialogue: 1,")]
    assert len(dialogues) == 1
    assert "0:00:20.78,0:00:24.20" in dialogues[0]

    meta = blur.read_ass_blur_metadata(ass_text)
    combined = blur.combine_blur_boxes(meta["boxes"], meta["text_boxes"])
    regions = blur.blur_regions(combined, 720, 1280, meta["settings"])
    # Entire 20.780..24.200s window coalesces into ONE single motionless GPU blur rectangle!
    assert len(regions) == 1
    assert regions[0][4] == [(pytest.approx(20.78), pytest.approx(24.20))]


def test_secondary_band_rejects_tilted_packaging_and_rescues_split_single_cjk():
    pts = [i / 10.0 for i in range(25)]

    class Capture:
        index = 0
        def set(self, prop, value):
            self.index = 0
            return True
        def read(self):
            if self.index >= len(pts):
                return False, None
            img = np.full((1280, 720, 3), self.index, dtype=np.uint8)
            self.index += 1
            return True, img
        def get(self, prop):
            return {
                cv2.CAP_PROP_FRAME_COUNT: len(pts),
                cv2.CAP_PROP_POS_FRAMES: self.index,
                cv2.CAP_PROP_POS_MSEC: pts[max(0, self.index - 1)] * 1000.0,
            }.get(prop, 0)

    def recognize(images):
        out = []
        for frame in images:
            idx = int(frame[0, 0, 0])
            if idx < 8:
                out.append([([[320, 245], [502, 245], [502, 292], [320, 292]], "可以立在桌上", 0.99)])
            elif idx < 11:
                # Handheld tilted Muji box label at secondary Y ~ 0.33 (68x75 px) alongside upper subtitle
                out.append([
                    ([[320, 245], [502, 245], [502, 292], [320, 292]], "可以立在桌上", 0.99),
                    ([[139, 395], [207, 395], [207, 469], [139, 469]], "小物件盒/大", 0.975),
                ])
            elif idx < 18:
                # Split single CJK '大' + '点的文具也能放' across missed thin stroke '一'
                out.append([
                    ([[172, 377], [205, 377], [205, 423], [172, 423]], "大", 0.86),
                    ([[242, 377], [490, 377], [490, 423], [242, 423]], "点的文具也能放", 0.998),
                ])
            else:
                out.append([([[119, 442], [329, 442], [329, 488], [119, 488]], "非常的硬史风呀", 0.998)])
        return out

    segs = [
        NS(index=6, start=timedelta(seconds=0.0), end=timedelta(seconds=1.8),
           content="但是当笔筒还挺好用的可以立在桌上大一点的文具也能放", paraformer_alt=""),
        NS(index=7, start=timedelta(seconds=1.8), end=timedelta(seconds=2.5),
           content="非常的硬式疯呀", paraformer_alt="非常的应实风雅"),
    ]
    band = NS(top=0.19, bottom=0.23, support=8, mode="asr_match", selected_by_segment={})
    with patch.object(ocr, "_readtext_batch", side_effect=recognize), \
         patch.object(ocr, "detect_static_watermarks", return_value=[]):
        tracks, _ = ocr._perform_visual_subtitle_ocr(
            Capture(), 720, 1280, 2.5, [], lambda: False,
            source_band=band, source_segments=segs,
        )
    assert all("小物件盒" not in t.text for t in tracks)
    clause_tracks = [t for t in tracks if "点的文具也能放" in t.text]
    assert clause_tracks
    assert min(t.x_pct for t in clause_tracks) <= 0.245


def test_attach_source_tracks_compensates_unconsumed_clause_prefix_without_filler_false_positive():
    seg6 = NS(
        index=6,
        start=timedelta(seconds=11.66),
        end=timedelta(seconds=15.14),
        content="但是当笔筒还挺好用的可以立在桌上大一点的文具也能放",
        paraformer_alt="",
    )
    seg9 = NS(
        index=9,
        start=timedelta(seconds=18.08),
        end=timedelta(seconds=20.64),
        content="这个只能放三支笔嗯 谁定制的极简风",
        paraformer_alt="",
    )
    tracks = [
        _trk("这个是眼镜盒但是当笔筒还挺好用的", 11.64, 12.633, 0.257, 0.889, top=0.190, bottom=0.228),
        _trk("可以立在桌上", 12.633, 13.700, 0.446, 0.698, top=0.192, bottom=0.228),
        _trk("点的文具也能放", 13.933, 15.200, 0.336, 0.681, top=0.294, bottom=0.331),
        _trk("这个只能放三支笔", 18.067, 19.267, 0.330, 0.660, top=0.190, bottom=0.226),
        _trk("谁定制的极简风", 19.333, 20.767, 0.520, 0.822, top=0.179, bottom=0.218),
    ]
    ocr._attach_source_caption_tracks([seg6, seg9], tracks)

    # '大一' (2 chars) before '点的文具也能放' expands x_pct left from 0.336 -> ~0.225
    assert seg6.tracking_blocks[-1].x_pct <= 0.245
    # Pure modal filler '嗯' before '谁定制的极简风' is NOT expanded
    assert seg9.tracking_blocks[-1].x_pct == pytest.approx(0.520)

