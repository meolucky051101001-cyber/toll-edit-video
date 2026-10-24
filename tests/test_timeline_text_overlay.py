"""Test builder ASS cho text clips + mapper timeline→output + decode ảnh overlay (P3)."""
import base64
import sys
import tempfile
import unittest
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent / "backend"
sys.path.insert(0, str(BASE_DIR))

from workflow_api import (  # noqa: E402
    _ass_time,
    _build_text_ass_clips,
    _decode_overlay_assets,
    _hex_to_ass_color,
    _timeline_to_output_t,
)


def _graph_for(clips):
    from workflow_api import _build_timeline_filter_graph
    return _build_timeline_filter_graph(clips, has_audio=True, src_w=1080, src_h=1920)


class TestAssColor(unittest.TestCase):
    def test_bgr_and_alpha(self):
        self.assertEqual(_hex_to_ass_color("#FF8800"), "&H000088FF")
        self.assertEqual(_hex_to_ass_color("#FFFFFF"), "&H00FFFFFF")
        self.assertEqual(_hex_to_ass_color("#000000"), "&H00000000")
        self.assertEqual(_hex_to_ass_color("rubbish"), "&H00FFFFFF")
        # '#80FF00': R=80 G=FF B=00 → BGR = 00 FF 80
        self.assertEqual(_hex_to_ass_color("#80FF00", alpha="40"), "&H4000FF80")

    def test_ass_time_format(self):
        self.assertEqual(_ass_time(0), "0:00:00.00")
        self.assertEqual(_ass_time(3.5), "0:00:03.50")
        self.assertEqual(_ass_time(61.234), "0:01:01.23")
        self.assertEqual(_ass_time(-2), "0:00:00.00")


class TestBuildTextAss(unittest.TestCase):
    def _clip(self, **kw):
        c = {
            "id": "t1", "text": "Xin chào", "at": 1.0, "dur": 2.0,
            "pos": {"x": 0.5, "y": 0.5},
            "style": {"fontSize": 80, "color": "#FFEB3B", "outlineColor": "#000000",
                      "outlineWidth": 3, "bgOpacity": 0, "bold": True},
            "animIn": "none", "animOut": "none",
        }
        c.update(kw)
        return c

    def test_header_and_playres(self):
        s = _build_text_ass_clips([self._clip()], 1080, 1920)
        self.assertIn("[Script Info]", s)
        self.assertIn("PlayResX: 1080", s)
        self.assertIn("PlayResY: 1920", s)
        self.assertIn("[Events]", s)

    def test_pos_maps_normalized_to_playres_center(self):
        s = _build_text_ass_clips([self._clip(pos={"x": 0.5, "y": 0.5})], 1080, 1920)
        # override PHẢI bọc trong {} — nếu không libass render thành chữ thường
        self.assertIn("{\\an5\\pos(540,960)}", s)
        self.assertIn("Dialogue: 0,0:00:01.00,0:00:03.00,TC0", s)
        # và Dialogue không được chứa tag nào nằm ngoài cặp ngoặc
        dlg = [ln for ln in s.splitlines() if ln.startswith("Dialogue:")][0]
        self.assertNotIn("}Xin chào\\", dlg)

    def test_background_box_uses_border_style_3(self):
        c = self._clip()
        c["style"]["bgOpacity"] = 0.75
        s = _build_text_ass_clips([c], 1080, 1920)
        style_line = [ln for ln in s.splitlines() if ln.startswith("Style: TC0")][0]
        self.assertIn(",3,", style_line)  # BorderStyle=3
        # alpha nền = (1-0.75)*255 ≈ 64 → 40 hex
        self.assertIn("&H40000000", style_line)

    def test_no_background_uses_border_style_1(self):
        s = _build_text_ass_clips([self._clip()], 1080, 1920)
        style_line = [ln for ln in s.splitlines() if ln.startswith("Style: TC0")][0]
        self.assertIn(",1,", style_line)

    def test_animations(self):
        s = _build_text_ass_clips([
            self._clip(animIn="fade", animOut="fade"),
            self._clip(animIn="slideUp"),
            self._clip(animIn="pop"),
        ], 1080, 1920)
        self.assertIn("{\\an5\\pos(540,960)\\fad(250,250)}", s)
        # slideUp: move từ dưới lên (y+140 → y), move thay pos
        self.assertIn("{\\an5\\move(540,1100,540,960,0,250)}", s)
        self.assertIn("{\\an5\\pos(540,960)\\fscx40\\fscy40\\t(0,220,\\fscx100\\fscy100)}", s)

    def test_multiline_and_brace_escape(self):
        s = _build_text_ass_clips([self._clip(text="dong 1\ndong 2 {x}")], 1080, 1920)
        self.assertIn("dong 1\\Ndong 2 (x)", s)

    def test_fontsize_in_style(self):
        s = _build_text_ass_clips([self._clip()], 1080, 1920)
        style_line = [ln for ln in s.splitlines() if ln.startswith("Style: TC0")][0]
        self.assertIn(",Arial,80,", style_line)


class TestTimelineToOutput(unittest.TestCase):
    def test_gap_squeeze_mapping(self):
        clips = [
            {"start": 0.0, "end": 10.0, "at": 0.0},
            {"start": 20.0, "end": 30.0, "at": 20.0},  # gap 10s trên timeline
        ]
        g = _graph_for(clips)
        self.assertEqual(g["seg_starts"], [0.0, 10.0])
        self.assertEqual(_timeline_to_output_t(clips, g, 5.0), 5.0)    # trong clip 1
        self.assertEqual(_timeline_to_output_t(clips, g, 25.0), 15.0)  # giữa clip 2
        self.assertEqual(_timeline_to_output_t(clips, g, 15.0), 10.0)  # trong gap → đầu clip 2
        self.assertEqual(_timeline_to_output_t(clips, g, 35.0), 20.0)  # quá cuối → cuối clip 2
        self.assertEqual(_timeline_to_output_t(clips, g, 0.0), 0.0)

    def test_speed_affects_mapping(self):
        clips = [{"start": 0.0, "end": 10.0, "at": 0.0, "speed": 2.0}]
        g = _graph_for(clips)
        self.assertEqual(g["seg_durs"], [5.0])
        # timeline 4s = nguồn 8s chạy 2x → output 4s
        self.assertEqual(_timeline_to_output_t(clips, g, 4.0), 4.0)
        # timeline t nằm ngoài đoạn (đoạn chỉ chiếm 0..5) → kẹp về cuối output
        self.assertEqual(_timeline_to_output_t(clips, g, 8.0), 5.0)


class TestDecodeOverlayAssets(unittest.TestCase):
    def test_valid_png_dataurl(self):
        png_b64 = base64.b64encode(b"\x89PNG fake image bytes").decode()
        with tempfile.TemporaryDirectory() as td:
            paths = _decode_overlay_assets(
                [{"id": "ov_1", "src": f"data:image/png;base64,{png_b64}"}],
                Path(td),
            )
            self.assertTrue(paths[0].is_file())
            self.assertEqual(paths[0].suffix, ".png")
            self.assertEqual(paths[0].parent.name, "overlay_assets")
            self.assertEqual(paths[0].read_bytes(), b"\x89PNG fake image bytes")

    def test_invalid_src_returns_none(self):
        with tempfile.TemporaryDirectory() as td:
            paths = _decode_overlay_assets([{"id": "x", "src": "not-a-dataurl"}], Path(td))
            self.assertEqual(paths, [None])


if __name__ == "__main__":
    unittest.main()
