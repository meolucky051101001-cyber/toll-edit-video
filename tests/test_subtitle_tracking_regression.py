"""Regression tests for the real V2 OCR -> selection -> tracking -> ASS path."""
import importlib
import re
import sys
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import patch
from backend.ass_utils import generate_ass_file
from backend.ocr_subtitle_locator import select_chinese_subtitle_band


def row(text, t=1.0, sid=1, x=.3, y=.8):
    return dict(text=text, sample_time=t, sample_segment_id=sid,
                x_pct=x, max_x_pct=x+.3, y_pct=y, max_y_pct=y+.04, prob=.95)


def seg(i=1, text="今天我们学习做饭"):
    return NS(index=i, start=timedelta(seconds=(i-1)*2),
              end=timedelta(seconds=i*2), content=text, best_block=None, tracking_blocks=[])


class SubtitleRegressionTests(unittest.TestCase):
    def test_visual_onset_leads_asr_without_overlap_or_mutating_audio_times(self):
        first = seg(text="Câu trước")
        second = seg(2, text="Câu tiếp theo")
        first.source_segment_id = 1
        second.source_segment_id = 2
        first.tracking_blocks = [NS(start=0., end=2.1, x_pct=.3,
            max_x_pct=.7, y_pct=.6, max_y_pct=.65)]
        second.tracking_blocks = [NS(start=1.8, end=3.2, x_pct=.2,
            max_x_pct=.8, y_pct=.65, max_y_pct=.7)]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "lead.ass"
            generate_ass_file([first, second], [], path)
            lines = [l for l in path.read_text(encoding="utf-8-sig").splitlines()
                     if l.startswith("Dialogue: 1,")]
        self.assertIn("0:00:00.00,0:00:01.80", lines[0])
        self.assertIn("0:00:01.80,0:00:03.20", lines[1])
        self.assertEqual(first.end.total_seconds(), 2)
        self.assertEqual(second.start.total_seconds(), 2)

    def test_source_motion_never_moves_card_horizontally(self):
        item = seg(text="Phụ đề giữa video")
        item.tracking_blocks = [
            NS(start=0, end=1, x_pct=.1, max_x_pct=.4, y_pct=.6, max_y_pct=.64),
            NS(start=1, end=2, x_pct=.6, max_x_pct=.9, y_pct=.7, max_y_pct=.74),
        ]
        text = self.render(item)
        backgrounds = [l for l in text.splitlines() if l.startswith("Dialogue: 0,")]
        self.assertEqual(len(backgrounds), 2)
        for line in backgrounds:
            x = int(re.search(r"\\pos\((\d+),", line)[1])
            width = int(re.search(r"m (\d+) ", line)[1])
            self.assertEqual(x + width / 2, 360)
        for line in text.splitlines():
            if line.startswith("Dialogue: 1,"):
                self.assertIn(r"\pos(360,", line)

    def test_rounded_path_has_four_curves_inside_original_bounds(self):
        from backend.ass_utils import _rounded_box
        for w, h in [(400, 54), (400, 96), (4, 4)]:
            path = _rounded_box(w, h)
            self.assertEqual(path.split().count("b"), 4)
            values = [float(v) for v in re.findall(r"[0-9.]+", path)]
            self.assertEqual(max(values[::2]), w)
            self.assertEqual(max(values[1::2]), h)
            self.assertEqual(min(values[::2]), 0)
            self.assertEqual(min(values[1::2]), 0)

    def render(self, segment, w=720, h=1280):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/"test.ass"
            generate_ass_file([segment], [], path, play_res_x=w, play_res_y=h)
            return path.read_text(encoding="utf-8-sig")

    def test_changed_product_labels_do_not_seed_a_band(self):
        blocks = [row(text, sid=i) for i,text in enumerate(["天然配方", "天然配万", "天然配方"],1)]
        band = select_chinese_subtitle_band(blocks, {i:"现在开始准备" for i in range(1,4)},720,1280)
        self.assertEqual(band.support,0)

    def test_one_product_mention_does_not_validate_static_label(self):
        blocks = [row("天然配方", sid=i) for i in range(1,4)]
        band = select_chinese_subtitle_band(blocks, {1:"天然配方",2:"现在开始准备",3:"放入锅里翻炒"},720,1280)
        self.assertEqual(band.support,0)

    def test_frames_same_segment_are_not_composited_across_time(self):
        blocks=[row("今天我们",t=.5),row("学习做饭",t=1.5,x=.55)]
        band=select_chinese_subtitle_band(blocks,{1:"今天我们学习做饭"},720,1280)
        samples=band.selected_by_sample[1]
        self.assertEqual(len(samples),2)
        self.assertEqual([b["text"] for b in samples],["今天我们","学习做饭"])
        self.assertEqual(samples[0]["x_pct"],.3)
        self.assertEqual(samples[1]["x_pct"],.55)

    def test_package_near_subtitle_not_added_to_union(self):
        blocks=[row("今天我们学习做饭"),row("天然配方",y=.845)]
        band=select_chinese_subtitle_band(blocks,{1:"今天我们学习做饭"},720,1280)
        self.assertEqual(band.selected_by_segment[1]["text"],"今天我们学习做饭")

    def test_square_and_four_three_canvas_preserve_aspect(self):
        for w,h in [(1080,1080),(1440,1080)]:
            text=self.render(seg(),w,h)
            x=int(re.search(r"PlayResX: (\d+)",text)[1])
            y=int(re.search(r"PlayResY: (\d+)",text)[1])
            self.assertAlmostEqual(x/y,w/h)

    def test_wide_source_at_edges_is_fully_covered(self):
        item=seg(text="Ngắn")
        item.best_block=NS(x_pct=0.,max_x_pct=1.,y_pct=.8,max_y_pct=.9)
        text=self.render(item)
        bg=next(l for l in text.splitlines() if l.startswith("Dialogue: 0,"))
        x,y=map(int,re.search(r"\\pos\((\d+),(\d+)\)",bg).groups())
        width,height=map(int,re.search(r"m (\d+) [\d.]+ l [\d.]+ [\d.]+ b [\d.]+ [\d.]+ [\d.]+ (\d+)",bg).groups())
        self.assertEqual(x,0)
        self.assertEqual(width,720)
        self.assertLessEqual(y,.8*1280)
        self.assertGreaterEqual(y+height,.9*1280)

    def test_tracking_gap_and_split_do_not_use_expired_box(self):
        item=seg(text="Thử nghiệm")
        item.start=timedelta(seconds=1.2)
        item.end=timedelta(seconds=2.)
        item.tracking_blocks=[NS(start=0.,end=1.,x_pct=.1,max_x_pct=.4,y_pct=.8,max_y_pct=.85),
                              NS(start=1.6,end=2.,x_pct=.6,max_x_pct=.9,y_pct=.8,max_y_pct=.85)]
        text=self.render(item)
        bg=[l for l in text.splitlines() if l.startswith("Dialogue: 0,")]
        self.assertEqual(len(bg),1)
        self.assertIn("0:00:01.60,0:00:02.00",bg[0])
        texts=[l for l in text.splitlines() if l.startswith("Dialogue: 1,")]
        self.assertEqual(len(texts),1)
        self.assertIn("0:00:01.60,0:00:02.00",texts[0])

    def test_real_ocr_path_excludes_package_and_batches_every_segment(self):
        class Frame:
            shape=(1280,720,3)
            def __getitem__(self,key): return self
        cap=NS(isOpened=lambda:True,get=lambda key:{1:30,2:720,3:1280}[key],
               set=lambda *args:None,read=lambda:(True,Frame()),release=lambda:None)
        fake=NS(VideoCapture=lambda _:cap,CAP_PROP_FPS=1,CAP_PROP_FRAME_WIDTH=2,
                CAP_PROP_FRAME_HEIGHT=3,CAP_PROP_POS_MSEC=4)
        # Only model I/O is stubbed; execute production acquisition/selection/render.
        with patch.dict(sys.modules, {"cv2":fake}), patch.object(
            sys, "path", [str(Path(__file__).resolve().parents[1] / "backend")] + sys.path
        ):
            sys.modules.pop("backend.ocr_utils",None)
            module=importlib.import_module("backend.ocr_utils")
            jitter = [row("今天我们", t=.1, y=.70),
                      row("今天我们", t=.3, y=.703),
                      row("今天我们", t=.5, y=.699),
                      row("学习做饭", t=.7, y=.82)]
            stable = module.stabilize_samples(jitter)
            self.assertEqual(len({r["y_pct"] for r in stable[:3]}), 1)
            self.assertEqual(len({r["max_y_pct"] for r in stable[:3]}), 1)
            self.assertGreater(stable[3]["y_pct"], stable[2]["max_y_pct"])
            calls=[]
            package=([[10,950],[160,950],[160,1000],[10,1000]],"天然配方",.99)
            subtitle=([[220,950],[600,950],[600,1000],[220,1000]],"今天我们学习做饭",.9)
            def recognize(frames):
                calls.append(len(frames))
                return [[package,subtitle] for _ in frames]
            segments=[seg(i) for i in range(1,20)]
            with patch.object(module,"_readtext_batch",side_effect=recognize):
                module.perform_video_ocr("fixture.mp4",srt_segments=segments)
            self.assertGreaterEqual(sum(calls),190)
            self.assertLessEqual(sum(calls),19 * 14)
            self.assertLessEqual(max(calls),12)
            for item in segments:
                self.assertGreaterEqual(len(item.tracking_blocks),12)
                self.assertTrue(all(b.text=="今天我们学习做饭" for b in item.tracking_blocks))
                self.assertAlmostEqual(item.tracking_blocks[0].start,max(0,item.start.total_seconds()-.3))
                self.assertAlmostEqual(item.tracking_blocks[-1].end,item.end.total_seconds()+.3)
            text=self.render(segments[0])
            self.assertIn("Dialogue: 0,",text)
            # One missing OCR sample must not create a mid-sentence flash.
            intermittent = seg()
            with patch.object(module, "_readtext_batch", return_value=
                              [[subtitle]] * 3 + [[]] + [[subtitle]] * 8):
                module.perform_video_ocr("fixture.mp4", srt_segments=[intermittent])
            for previous, following in zip(intermittent.tracking_blocks,
                                           intermittent.tracking_blocks[1:]):
                self.assertAlmostEqual(previous.end, following.start)
            # Source vanishes after 0.7s while ASR continues until 2s.
            two_missing = seg()
            with patch.object(module, "_readtext_batch", return_value=
                              [[subtitle]] * 3 + [[]] * 2 + [[subtitle]] * 7):
                module.perform_video_ocr("fixture.mp4", srt_segments=[two_missing])
            for previous, following in zip(two_missing.tracking_blocks,
                                           two_missing.tracking_blocks[1:]):
                self.assertAlmostEqual(previous.end, following.start)
            early = seg()
            with patch.object(module, "_readtext_batch", return_value=
                              [[subtitle]] * 4 + [[]] * 8):
                module.perform_video_ocr("fixture.mp4", srt_segments=[early])
            self.assertAlmostEqual(early.tracking_blocks[-1].end,
                                   early.tracking_blocks[-1].sample_time + .4)
            rendered = self.render(early)
            events = [l for l in rendered.splitlines() if l.startswith("Dialogue:")]
            self.assertTrue(events)
            for event in events:
                end = event.split(",")[2]
                self.assertLessEqual(end, "0:00:01.10")
            sys.modules.pop("backend.ocr_utils",None)


if __name__=="__main__":
    unittest.main()
