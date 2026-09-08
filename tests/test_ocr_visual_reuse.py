import unittest
import importlib
import sys
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
import cv2
import numpy as np
from backend.ocr_frame_cache import SubtitleFrameCache
from backend.ocr_subtitle_locator import select_chinese_subtitle_band
from tests.test_ocr_subtitle_locator import block


class VisualReuseTests(unittest.TestCase):
    def test_transition_cover_includes_next_line_without_bridging_absence(self):
        from backend.ass_utils import _transition_cover_blocks
        rows = [dict(start=29.86,end=30.05,x_pct=.16,max_x_pct=.84,y_pct=.657,max_y_pct=.704),
                dict(start=30.05,end=30.24,x_pct=0.,max_x_pct=1.,y_pct=.673,max_y_pct=.724)]
        result = _transition_cover_blocks(rows)
        self.assertEqual(result[0]['x_pct'], 0.)
        self.assertEqual(result[0]['max_x_pct'], 1.)
        self.assertEqual(result[0]['max_y_pct'], .724)
        self.assertEqual(result[0]['end'], rows[0]['end'])
        rows[1]['start'] = 30.2
        self.assertEqual(_transition_cover_blocks(rows)[0], rows[0])

    def test_caption_spanning_two_asr_cues_is_not_mistaken_for_static_label(self):
        speech = {4:'教大家几个猩猩贴纸有趣玩法内页撕掉一角',
                  5:'然后粘上PVC纸并贴上猩猩',
                  6:'这样一个好看的M内页就完成了'}
        rows = [block(i, '内页撕掉一角然后粘上PVC纸并贴上星星', .05,.95,.675,.725)
                for i in (4,5,6)]
        result = select_chinese_subtitle_band(rows, speech,1080,1920)
        self.assertIn(5, result.selected_by_segment)

    def test_adaptive_acquisition_reduces_recognition_and_preserves_cover(self):
        with patch.object(sys, 'path', [str(Path(__file__).resolve().parents[1]/'backend')]+sys.path):
            module = importlib.import_module('backend.ocr_utils')
            frame = np.zeros((1280,720,3), dtype=np.uint8)
            frame[1015:1060,220:600] = 255
            cap = SimpleNamespace(isOpened=lambda: True,
                get=lambda key: {cv2.CAP_PROP_FPS:30, cv2.CAP_PROP_FRAME_WIDTH:720,
                    cv2.CAP_PROP_FRAME_HEIGHT:1280, cv2.CAP_PROP_FRAME_COUNT:180}[key],
                set=lambda *args: None, read=lambda: (True, frame.copy()), release=lambda: None)
            from backend.pipeline_v2.segments import RuntimeSegment
            segments = [RuntimeSegment(i+1, timedelta(seconds=i*2),
                timedelta(seconds=(i+1)*2), '今天我们学习做饭') for i in range(3)]
            calls = []
            def recognize(frames):
                calls.extend(frames)
                rows=[]
                for image in frames:
                    # Find the same fixture line regardless of ROI crop offset.
                    ys, xs = np.where(image[:,:,0] > 0)
                    rows.append([([[int(xs.min()),int(ys.min())], [int(xs.max()),int(ys.min())],
                        [int(xs.max()),int(ys.max())], [int(xs.min()),int(ys.max())]],
                        '今天我们学习做饭', .9)])
                return rows
            with patch.object(module.cv2, 'VideoCapture', return_value=cap), patch.object(
                    module, '_readtext_batch', side_effect=recognize):
                module.perform_video_ocr('fixture.mp4', srt_segments=segments)
            self.assertLessEqual(len(calls), 18)
            for item in segments:
                self.assertIsNotNone(item.best_block)
                self.assertTrue(item.tracking_blocks)
                self.assertAlmostEqual(item.best_block.y_pct, 1015/1280-.0046875, places=3)

    def test_unchanged_caption_reuses_but_change_absence_and_expiry_do_not(self):
        image = np.zeros((200, 360, 3), dtype=np.uint8)
        image[140:165, 70:85] = 255
        cache = SubtitleFrameCache(.6, .9)
        value = {"rows": ["source"]}
        cache.remember(cache.signature(image), 0, value)
        self.assertIs(cache.lookup(cache.signature(image), .2), value)
        changed = image.copy()
        changed[140:165, 130:145] = 255
        self.assertIsNone(cache.lookup(cache.signature(changed), .2))
        self.assertIsNone(cache.lookup(cache.signature(np.zeros_like(image)), .2))
        self.assertIsNone(cache.lookup(cache.signature(image), 1.2))

    def test_corrupt_line_between_confirmed_subtitles_recovers_observed_box(self):
        speech = {1: '教大家几个星星贴纸有趣玩法',
                  2: '然后粘上PVC纸并贴上猩猩',
                  3: '这样一个好看的内页就完成了'}
        rows = [block(1, speech[1], .15, .85, .67, .725),
                block(2, '肉弼唣愈屙豳凸纸飑凰屋', .05, .95, .675, .73, 0.00001),
                block(2, '产品包装说明', .3, .7, .4, .45),
                block(3, speech[3], .12, .88, .67, .725)]
        result = select_chinese_subtitle_band(rows, speech, 1080, 1920)
        self.assertEqual(result.selected_by_segment[2]['text'], rows[1]['text'])
        # An absent frame has no observed rectangle to recover.
        result = select_chinese_subtitle_band([rows[0], rows[2], rows[3]], speech, 1080, 1920)
        self.assertNotIn(2, result.selected_by_segment)
        result = select_chinese_subtitle_band(rows[1:3], speech, 1080, 1920)
        self.assertEqual(result.mode, 'default')

    def test_static_label_in_confirmed_band_is_not_recovered(self):
        speech = {1:'今天给大家介绍新的做法', 2:'接着把纸张剪开',
                  3:'然后贴上去就完成了', 4:'我们下次再见'}
        rows = [block(1, speech[1], .15, .85, .67, .72),
                block(4, speech[4], .15, .85, .67, .72)]
        rows += [block(i, '产品说明包装标签', .25, .75, .67, .72) for i in range(1,5)]
        result = select_chinese_subtitle_band(rows, speech, 1080, 1920)
        self.assertNotIn(2, result.selected_by_segment)
        self.assertNotIn(3, result.selected_by_segment)
