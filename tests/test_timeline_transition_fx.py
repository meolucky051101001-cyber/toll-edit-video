"""Test transition (xfade/acrossfade) + filter màu per-clip + re-time theo transition (P4)."""
import sys
import unittest
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent / "backend"
sys.path.insert(0, str(BASE_DIR))

from workflow_api import _build_timeline_filter_graph, _retime_subtitles_to_timeline  # noqa: E402


def _clip(start, end, at=None, **kw):
    c = {"start": start, "end": end}
    if at is not None:
        c["at"] = at
    c.update(kw)
    return c


def _joined(graph):
    return ";".join(graph["filter_lines"])


class TestTransitionGraph(unittest.TestCase):
    def test_two_transitions_shorten_output_and_use_xfade(self):
        g = _build_timeline_filter_graph([
            _clip(0, 4, at=0.0, trans={"type": "fade", "dur": 0.5}),
            _clip(4, 8, at=3.5, trans={"type": "wipeleft", "dur": 0.5}),
            _clip(8, 12, at=7.0),
        ], has_audio=True)
        self.assertEqual(g["seg_starts"], [0.0, 3.5, 7.0])
        self.assertAlmostEqual(g["total_dur"], 11.0, places=3)
        joined = _joined(g)
        self.assertIn("xfade=transition=fade:duration=0.500:offset=3.500", joined)
        self.assertIn("xfade=transition=wipeleft:duration=0.500:offset=7.000", joined)
        self.assertIn("acrossfade=d=0.500", joined)
        self.assertNotIn("concat=n=3", joined)
        self.assertIn("[outv]", joined) and self.assertIn("[outa]", joined)

    def test_mixed_chain_trans_and_plain_pairs(self):
        g = _build_timeline_filter_graph([
            _clip(0, 4, at=0.0, trans={"type": "fade", "dur": 0.5}),
            _clip(4, 8, at=3.5),
            _clip(8, 12, at=7.5),
        ], has_audio=True)
        joined = _joined(g)
        self.assertIn("xfade=transition=fade", joined)
        # cặp không có transition nối bằng concat n=2
        self.assertIn("concat=n=2:v=1:a=0", joined)
        self.assertAlmostEqual(g["total_dur"], 11.5, places=3)

    def test_trans_in_mix_mode_uses_amix_not_acrossfade(self):
        g = _build_timeline_filter_graph([
            _clip(0, 4, at=0.0, trans={"type": "fade", "dur": 0.5}, detachAudio=True),
            _clip(4, 8, at=3.5, detachAudio=True),
        ], has_audio=True)
        joined = _joined(g)
        self.assertIn("xfade=transition=fade", joined)
        self.assertIn("amix=inputs=2:", joined)
        self.assertIn("adelay=0:all=1", joined)
        self.assertIn("adelay=3500:all=1", joined)  # vị trí output sau khi rút ngắn
        self.assertNotIn("acrossfade", joined)
        self.assertAlmostEqual(g["total_dur"], 7.5, places=3)

    def test_trans_clamped_to_one_second(self):
        g = _build_timeline_filter_graph([
            _clip(0, 4, at=0.0, trans={"type": "fade", "dur": 5.0}),
            _clip(4, 8, at=3.0),
        ], has_audio=False)
        self.assertIn("xfade=transition=fade:duration=1.000", _joined(g))
        self.assertAlmostEqual(g["total_dur"], 7.0, places=3)

    def test_trans_on_last_clip_ignored(self):
        g = _build_timeline_filter_graph([
            _clip(0, 4, at=0.0),
            _clip(4, 8, at=4.0, trans={"type": "fade", "dur": 0.5}),
        ], has_audio=False)
        joined = _joined(g)
        self.assertNotIn("xfade", joined)
        self.assertIn("concat=n=2:v=1:a=0[outv]", joined)
        self.assertAlmostEqual(g["total_dur"], 8.0, places=3)

    def test_single_clip_never_xfade(self):
        g = _build_timeline_filter_graph([_clip(0, 4, at=0.0, trans={"type": "fade", "dur": 0.5})], has_audio=True)
        self.assertNotIn("xfade", _joined(g))


class TestFxFilters(unittest.TestCase):
    def test_full_fx_chain_after_yuv420p_before_crop(self):
        g = _build_timeline_filter_graph([
            _clip(0, 5, fx={"brightness": 0.1, "contrast": 1.2, "saturation": 1.5,
                            "temperature": 0.5, "blur": 2, "vignette": True},
                  crop={"x": 0.1, "y": 0.1, "w": 0.8, "h": 0.8}),
        ], has_audio=False, src_w=1080, src_h=1920)
        chain = [ln for ln in g["filter_lines"] if ln.startswith("[0:v]")][0]
        self.assertIn("eq=brightness=0.100:contrast=1.200:saturation=1.500", chain)
        self.assertIn("colorbalance=rs=0.150:bs=-0.150", chain)
        self.assertIn("boxblur=2", chain)
        self.assertIn("vignette=PI/5", chain)
        self.assertLess(chain.index("format=yuv420p"), chain.index("eq="))
        self.assertLess(chain.index("eq="), chain.index("crop="))

    def test_neutral_fx_omitted(self):
        g = _build_timeline_filter_graph([_clip(0, 5, fx={"brightness": 0, "contrast": 1})], has_audio=False)
        self.assertNotIn("eq=", _joined(g))

    def test_no_fx_key_no_filters(self):
        g = _build_timeline_filter_graph([_clip(0, 5)], has_audio=False)
        self.assertNotIn("eq=", _joined(g))
        self.assertNotIn("vignette", _joined(g))


class TestRetimeWithTransition(unittest.TestCase):
    def test_sub_shifted_by_transition_shortening(self):
        subs = [{"start": 4.0, "end": 6.0, "text": "a"}, {"start": 14.0, "end": 16.0, "text": "b"}]
        clips = [
            _clip(0, 10, at=0.0, trans={"type": "fade", "dur": 1.0}),
            _clip(10, 20, at=9.0),
        ]
        out = _retime_subtitles_to_timeline(subs, [], clips)
        self.assertEqual(len(out), 2)
        # sub trong clip 1: 4..6 (không đổi)
        self.assertEqual(out[0]["start"], 4.0)
        # sub trong clip 2: output bắt đầu ở 9 (= 10 - 1 transition) → 14→13, 16→15
        self.assertEqual(out[1]["start"], 13.0)
        self.assertEqual(out[1]["end"], 15.0)

    def test_retime_without_trans_unchanged(self):
        subs = [{"start": 12.0, "end": 14.0, "text": "x"}]
        clips = [_clip(0, 10, at=0.0), _clip(10, 20, at=10.0)]
        out = _retime_subtitles_to_timeline(subs, [], clips)
        self.assertEqual(out[0]["start"], 12.0)


if __name__ == "__main__":
    unittest.main()
