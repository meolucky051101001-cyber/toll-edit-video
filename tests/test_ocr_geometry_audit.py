"""Caption selection and source-mask regressions without model or GPU use."""
import re
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace as NS

from backend.ass_utils import generate_ass_file
from backend.ocr_subtitle_locator import select_chinese_subtitle_band
from backend.pipeline_v2.cover_qc import parse_ass_covers


SPEECH = "\u4eca\u5929\u6211\u4eec\u5b66\u4e60\u505a\u996d"


def row(text=SPEECH, sid=1, time=.5, left=.1, right=.9, top=.75, bottom=.8, prob=.95):
    return dict(text=text, sample_segment_id=sid, sample_time=time,
                start=time-.2, end=time+.2, x_pct=left, max_x_pct=right,
                y_pct=top, max_y_pct=bottom, prob=prob)


def track(start, end, left=.1, right=.9, top=.75, bottom=.8, verified=True):
    return NS(start=start, end=end, x_pct=left, max_x_pct=right,
              y_pct=top, max_y_pct=bottom, geometry_verified=verified)


class CaptionSelectionAuditTests(unittest.TestCase):
    def select(self, rows, speech=None):
        return select_chinese_subtitle_band(rows, speech or {1: SPEECH}, 1080, 1920)

    def test_wide_matched_caption_keeps_observed_bounds(self):
        selected = self.select([row(left=0, right=1)]).selected_by_segment[1]
        self.assertEqual((selected['x_pct'], selected['max_x_pct']), (0, 1))

    def test_caption_observed_at_neighbour_boundaries_is_not_a_static_label(self):
        speech = {1: '\u73b0\u5728\u5f00\u59cb\u51c6\u5907', 2: SPEECH,
                  3: '\u7136\u540e\u653e\u5165\u9505\u91cc'}
        rows = [dict(row(sid=i), sample_in_cue=(i == 2)) for i in (1, 2, 3)]
        self.assertIn(2, self.select(rows, speech).selected_by_segment)
        for item in rows:
            item['sample_in_cue'] = True
        self.assertNotIn(2, self.select(rows, speech).selected_by_segment)

    def test_large_matched_caption_is_not_discarded_by_height(self):
        result = self.select([row(left=.02, right=.98, top=.8, bottom=1.)])
        self.assertIn(1, result.selected_by_segment)
        result = self.select([row(text='\u4ea7\u54c1\u5305\u88c5\u53c2\u6570', left=.02,
                                  right=.98, top=.4, bottom=.6)])
        self.assertEqual(result.support, 0)

    def test_wide_unmatched_scene_text_does_not_seed_a_band(self):
        result = self.select([row(text="\u4ea7\u54c1\u5305\u88c5\u53c2\u6570", left=0, right=1, top=.4, bottom=.45)])
        self.assertEqual(result.support, 0)

    def test_account_handle_does_not_expand_a_caption(self):
        for prefix in ('@', '\uff20'):
            result = self.select([row(left=.1, right=.5),
                                  row(text=prefix+'\u4e0d\u4e8c', left=.52, right=.7)])
            self.assertEqual(result.selected_by_segment[1]['max_x_pct'], .5)

    def test_two_lines_cover_full_fifteen_percent_height(self):
        result = self.select([row(text=SPEECH[:4], top=.6, bottom=.67),
                              row(text=SPEECH[4:], top=.68, bottom=.75)])
        selected = result.selected_by_segment[1]
        self.assertEqual((selected['y_pct'], selected['max_y_pct']), (.6, .75))

    def test_low_confidence_row_requires_validated_neighbours(self):
        speech = {1: SPEECH, 2: '\u7136\u540e\u51c6\u5907\u6750\u6599', 3: '\u653e\u5165\u9505\u91cc\u7ffb\u7092'}
        corrupt = row(text='\u8089\u5f3c\u5523\u6108\u5c59\u8c73\u51f8\u7eb8', sid=2, time=2.5, prob=.00001)
        rows = [row(), corrupt, row(text=speech[3], sid=3, time=4.5)]
        self.assertIn(2, self.select(rows, speech).selected_by_segment)
        self.assertNotIn(2, self.select(rows[:2], speech).selected_by_segment)
        self.assertNotIn(2, self.select([rows[0], rows[2]], speech).selected_by_segment)


class SourceMaskAuditTests(unittest.TestCase):
    def render(self, tracks, *, best=None, verified=True, size=(1080, 1920)):
        segment = NS(index=1, start=timedelta(), end=timedelta(seconds=3),
                     content='Phu de tieng Viet', geometry_verified=verified,
                     best_block=best or (tracks[0] if tracks else None), tracking_blocks=tracks)
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)/'audit.ass'
            generate_ass_file([segment], [], output, *size, video_duration=3)
            content = output.read_text(encoding='utf-8-sig')
        return content, parse_ass_covers(content)

    def test_source_mask_can_reach_every_frame_edge(self):
        for size in ((1080, 1920), (1920, 1080), (1080, 1080)):
            for top, bottom in ((.1, .15), (.85, .9), (.95, 1)):
                with self.subTest(size=size, top=top):
                    content, (covers, width, height) = self.render(
                        [track(0, 3, left=0, right=1, top=top, bottom=bottom)], size=size)
                    self.assertTrue(covers)
                    for _, _, x1, y1, x2, y2 in covers:
                        self.assertLessEqual(x1, 0)
                        self.assertGreaterEqual(x2, width)
                        self.assertLessEqual(y1, top*height)
                        self.assertGreaterEqual(y2, bottom*height)
                    positions = re.findall(r'Dialogue: 1,.*?\\pos\(([\d.]+),', content)
                    self.assertTrue(all(.05*width <= float(x) <= .95*width for x in positions))

    def test_confirmed_wide_best_block_is_not_treated_as_fallback(self):
        _, (covers, width, _) = self.render([], best=track(0, 3, left=.01, right=.99, top=.1, bottom=.15), verified=False)
        self.assertLessEqual(covers[0][2], .01*width)
        self.assertGreaterEqual(covers[0][4], .99*width)

    def test_verified_absence_does_not_bridge_or_hold_mask(self):
        content, (covers, _, _) = self.render([track(.4, 1), track(1.8, 2.2)])
        self.assertEqual([(start, end) for start, end, *_ in covers], [(.4, 1), (1.8, 2.2)])
        self.assertIn('Dialogue: 1,0:00:00.00,0:00:00.40', content)
        self.assertIn('Dialogue: 1,0:00:01.00,0:00:01.80', content)
        self.assertIn('Dialogue: 1,0:00:02.20,0:00:03.00', content)

    def test_verified_empty_tracks_never_resurrect_best_block(self):
        _, (covers, _, _) = self.render([], best=track(0, 3))
        self.assertEqual(covers, [])

    def test_overlap_covers_every_active_observed_box(self):
        _, (covers, width, height) = self.render([
            track(0, 2, left=.01, right=.45, top=.5, bottom=.55),
            track(1, 3, left=.55, right=.99, top=.6, bottom=.65)])
        overlap = next(cover for cover in covers if cover[0] == 1 and cover[1] == 2)
        self.assertLessEqual(overlap[2], .01*width)
        self.assertGreaterEqual(overlap[4], .99*width)
        self.assertLessEqual(overlap[3], .5*height)
        self.assertGreaterEqual(overlap[5], .65*height)

    def test_source_lifetime_outside_speech_is_covered_without_extending_text(self):
        segment = NS(index=1, start=timedelta(seconds=1), end=timedelta(seconds=2),
                     content='Ban dich', geometry_verified=True, ocr_mask_status='located',
                     best_block=track(.5, 2.5), tracking_blocks=[track(.5, 2.5)])
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)/'lifetime.ass'
            generate_ass_file([segment], [], output, video_duration=3)
            content = output.read_text(encoding='utf-8-sig')
        covers, _, _ = parse_ass_covers(content)
        self.assertEqual(sorted((a,b) for a,b,*_ in covers), [(.5,1), (1,2), (2,2.5)])
        texts = [line for line in content.splitlines() if line.startswith('Dialogue: 1,')]
        self.assertEqual(len(texts), 1)
        self.assertIn('0:00:01.00,0:00:02.00', texts[0])

    def test_verified_geometry_classification_survives_serialization(self):
        from backend.pipeline_v2.segments import segment_to_dict, segment_from_dict
        segment = NS(index=1, start=timedelta(), end=timedelta(seconds=2),
                     content='Ban dich', geometry_verified=True, ocr_mask_status='located',
                     best_block=track(0,2), tracking_blocks=[track(0,2)])
        restored = segment_from_dict(segment_to_dict(segment))
        self.assertTrue(restored.geometry_verified)
        self.assertEqual(restored.ocr_mask_status, 'located')
        self.assertTrue(restored.tracking_blocks[0].geometry_verified)
        segment.best_block = vars(segment.best_block)
        segment.tracking_blocks = [vars(block) for block in segment.tracking_blocks]
        restored = segment_from_dict(segment_to_dict(segment))
        self.assertEqual(restored.best_block.x_pct, .1)
        self.assertEqual(restored.best_block.max_x_pct, .9)
        self.assertEqual(restored.tracking_blocks[0].end, 2)


if __name__ == '__main__':
    unittest.main()
