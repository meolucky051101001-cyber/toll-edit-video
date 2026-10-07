"""Offline geometry regressions; no providers, model loads or production jobs."""
import ast
import importlib.util
import json
import math
import os
import re
import sys
import tempfile
import types
import unittest
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

import numpy as np
import cv2

CODE = Path(os.environ.get('V1_OCR_GEOMETRY_TEST_CODE', Path(__file__).resolve().parents[1] / 'backend'))
PROD = Path(r'C:\tool v1\backend')
sys.path[:0] = [str(CODE), str(PROD)]
sys.modules['v1_stage_metrics'] = types.SimpleNamespace(stage=lambda *a, **k:lambda f:f)
sys.modules['v1_ocr_proxy'] = types.SimpleNamespace(ocr_proxy=lambda f:f)
sys.modules['shared_state'] = types.SimpleNamespace(stop_requested=False)
sys.modules['ai.v1_model_policy'] = types.SimpleNamespace(current_v1_model_policy=lambda:None)
sys.modules['ai.v1_model_runtime'] = types.SimpleNamespace(V1ModelRuntimeError=RuntimeError,
    run_v1_ocr=lambda *a, **k:None, runtime_module_available=lambda *a:False)


def load(name, path):
    source = CODE / path if (CODE / path).is_file() else PROD / path
    spec = importlib.util.spec_from_file_location(name, source)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


geo = load('v1_ocr_geometry', 'v1_ocr_geometry.py')
locator = load('ocr_subtitle_locator', 'ocr_subtitle_locator.py')
ocr = load('ocr_utils', 'ocr_utils.py')
ass = load('ass_utils', 'ass_utils.py')
runtime = load('v1_stage_runtime', 'v1_stage_runtime.py')


def block(text='这是当时导致伤口感染', sid=1, t=1., x=.1, right=.8, y=.9, bottom=.99):
    return dict(text=text, sample_segment_id=sid, sample_time=t,
                x_pct=x, max_x_pct=right, y_pct=y, max_y_pct=bottom, prob=.95,
                start=t-.3, end=t+.3)


def segment(index=1, content='这是当时导致伤口感染', duration=2.):
    return types.SimpleNamespace(index=index, content=content,
        start=timedelta(seconds=(index-1)*duration), end=timedelta(seconds=index*duration))


class Capture:
    def __init__(self, width=720, height=1280, duration=600., readable=True):
        self.width, self.height, self.duration, self.readable = width, height, duration, readable
        self.timestamps, self.released = [], False
    def isOpened(self): return True
    def get(self, key):
        return {cv2.CAP_PROP_FRAME_WIDTH:self.width, cv2.CAP_PROP_FRAME_HEIGHT:self.height,
                cv2.CAP_PROP_FPS:30., cv2.CAP_PROP_FRAME_COUNT:self.duration*30}.get(key, 0)
    def set(self, key, value): self.timestamps.append(value/1000.)
    def read(self): return (True, np.zeros((self.height,self.width,3),np.uint8)) if self.readable else (False,None)
    def release(self): self.released=True


class GeometryTests(unittest.TestCase):
    def select(self, blocks, texts): return locator.select_chinese_subtitle_band(blocks,texts,1920,1080)
    def test_bottom_edge_not_rejected(self):
        b=block(bottom=1.)
        band=self.select([b],{1:b['text']})
        self.assertEqual(band.selected_by_segment[1]['max_y_pct'],1.)
    def test_top_edge_not_rejected(self):
        b=block(y=0.,bottom=.06)
        self.assertIn(1,self.select([b],{1:b['text']}).selected_by_segment)
    def test_full_row_covered_for_partial_asr_sentence(self):
        rows=[block('这是当时导致伤口感染',x=.02,right=.49),
              block('最常见',x=.51,right=.66), block('致命的细菌',x=.68,right=.98)]
        selected=self.select(rows,{1:'这是当时导致伤口感染'}).selected_by_segment[1]
        self.assertEqual(selected['x_pct'],.02)
        self.assertEqual(selected['max_x_pct'],.98)
    def test_different_timestamps_not_unioned(self):
        rows=[block(x=.1,right=.4),block(t=1.8,x=.6,right=.9)]
        selected=self.select(rows,{1:rows[0]['text']}).selected_by_sample[1]
        self.assertEqual([r['x_pct'] for r in selected],[.1,.6])
    def test_real_red_caption_padded_fragments_cover_entire_row(self):
        rows=geo.normalized_rows([
            ([[8,482],[485,484],[485,531],[8,529]],'这是当时导致伤口感染，',.99),
            ([[500,485],[704,485],[704,530],[500,530]],'最常见',.99),
            ([[685,487],[933,487],[933,529],[685,529]],'致命的细菌！',.99)
        ],.5,0,1920,1080)
        for row in rows:row.update(sample_segment_id=7,sample_time=15.265)
        selected=self.select(rows,{7:'这是当时导致伤口感染'}).selected_by_segment[7]
        self.assertEqual(selected['x_pct'],0.)
        self.assertGreater(selected['max_x_pct'],.98)
    def test_different_rows_not_unioned(self):
        rows=[block(y=.8,bottom=.84),block('商品品牌',x=.7,right=.9,y=.845,bottom=.88)]
        selected=self.select(rows,{1:rows[0]['text']}).selected_by_segment[1]
        self.assertEqual(selected['max_y_pct'],.84)
    def test_static_labels_do_not_seed_band(self):
        rows=[block('纯天然配方',sid=i) for i in range(1,4)]
        self.assertEqual(self.select(rows,{i:'现在开始准备' for i in range(1,4)}).support,0)
    def test_static_neighbour_not_added(self):
        rows=[]
        texts={1:'这是当时导致伤口感染',2:'今天我们学习做饭',3:'现在开始准备材料'}
        for i,text in texts.items():
            rows.extend([block(text,sid=i,x=.02,right=.5),block('纯天然配方',sid=i,x=.52,right=.75)])
        selected=self.select(rows,texts).selected_by_segment[1]
        self.assertEqual(selected['max_x_pct'],.5)
    def test_handle_not_added(self):
        rows=[block(x=.02,right=.5),block('@不二',x=.52,right=.7)]
        self.assertEqual(self.select(rows,{1:rows[0]['text']}).selected_by_segment[1]['max_x_pct'],.5)
    def test_invalid_nan_box_rejected(self):
        self.assertFalse(geo.valid_box(block(y=math.nan)))
    def test_invalid_inverted_box_rejected(self):
        self.assertFalse(geo.valid_box(block(x=.8,right=.1)))
    def test_sample_plan_never_leaves_cue(self):
        self.assertTrue(all(14.78<t<16.72 for t in geo.sample_times(14.78,16.72,2)))
    def test_zero_duration_plan_empty(self):
        self.assertEqual(geo.sample_times(1,1,2),[])
    def test_rescue_two_in_cue_samples(self):
        self.assertEqual(geo.rescue_times(0,2),[.36,1.64])
    def test_crop_and_resize_coordinates_restored(self):
        rows=[([[10,20],[110,20],[110,40],[10,40]],'文字',.9)]
        b=geo.normalized_rows(rows,.5,800,1920,1080)[0]
        self.assertAlmostEqual(b['y_pct'],(840-5.4)/1080)
        self.assertAlmostEqual(b['max_x_pct'],(220+19.2)/1920)
    def test_bottom_margin_clamped_to_frame(self):
        b=geo.normalized_rows([([[0,97],[100,97],[100,100],[0,100]],'文字',.9)],1,0,100,100)[0]
        self.assertEqual(b['max_y_pct'],1.)
    def test_unresolved_fails_before_ass_write(self):
        s=segment();s.ocr_mask_status='unresolved'
        with tempfile.TemporaryDirectory() as tmp:
            out=Path(tmp)/'unsafe.ass'
            with self.assertRaises(geo.OCRGeometryError):ass.generate_ass_file([s],[],out)
            self.assertFalse(out.exists())
    def test_missing_box_not_reported_located(self):
        s=segment();s.ocr_mask_status='located';s.best_block=None
        with self.assertRaises(geo.OCRGeometryError):geo.validate_mask_geometry([s])
    def test_no_caption_keeps_vietnamese_card(self):
        s=segment(content='Không có chữ gốc');s.ocr_mask_status='no_caption_evidence'
        with tempfile.TemporaryDirectory() as tmp:
            out=Path(tmp)/'safe.ass';ass.generate_ass_file([s],[],out)
            self.assertIn('Không có chữ gốc',out.read_text(encoding='utf-8-sig'))
    def test_mask_covers_bottom_box(self):
        s=segment(content='Bản dịch');s.ocr_mask_status='located';s.best_block=types.SimpleNamespace(**block(y=.9,bottom=1.))
        with tempfile.TemporaryDirectory() as tmp:
            out=Path(tmp)/'safe.ass';ass.generate_ass_file([s],[],out,play_res_x=1920,play_res_y=1080)
            line=next(l for l in out.read_text(encoding='utf-8-sig').splitlines() if l.startswith('Dialogue: 0,'))
            y=int(re.search(r'\\pos\(\d+,(\d+)\)',line)[1])
            self.assertLessEqual(y,.9*720)
            self.assertIn('BgStyle',line)
    def test_status_persisted_in_checkpoint(self):
        s=segment();s.ocr_mask_status='located';s.best_block=types.SimpleNamespace(**block())
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'ocr.json'
            runtime.save_payload(path,segments=[s],geometry_version=geo.OCR_GEOMETRY_VERSION)
            saved=runtime.load_payload(path)
            self.assertEqual(saved['segments'][0].ocr_mask_status,'located')
            self.assertEqual(saved['geometry_version'],3)
    def test_resume_invalidates_old_ocr_not_audio_asr(self):
        source=(CODE/'v1_orchestrator.py').read_text(encoding='utf-8-sig')
        tree=ast.parse(source)
        condition=next(n for n in ast.walk(tree) if isinstance(n,ast.If) and 'cached_geometry != OCR_GEOMETRY_VERSION'==ast.unparse(n.test))
        self.assertIn("STAGES_ORDER.index('visual_ocr')",ast.unparse(condition))
        self.assertIn('STAGES_ORDER.index(s) < ocr_pos',ast.unparse(condition))
    def test_ambiguous_edge_caption_not_reported_subtitle_free(self):
        band=self.select([],{1:'字幕'})
        self.assertTrue(geo.has_caption_evidence([block('无法识别',sid=2)],2,band))
    def test_unrelated_middle_label_not_called_caption(self):
        band=self.select([],{1:'字幕'})
        self.assertFalse(geo.has_caption_evidence([block('商品品牌',sid=2,y=.4,bottom=.45)],2,band))
    def test_unmatched_caption_evidence_is_not_silent_success(self):
        band=self.select([block()],{1:'这是当时导致伤口感染'})
        self.assertTrue(geo.has_caption_evidence([block('不同字幕',sid=2)],2,band))


class PipelineTests(unittest.TestCase):
    def run_ocr(self, segments, recognize, **kwargs):
        captures=[]
        capture_args = kwargs.pop('capture_args',{})
        def capture(_):
            c=Capture(**capture_args);captures.append(c);return c
        with patch.object(ocr.cv2,'VideoCapture',side_effect=capture), patch.object(ocr,'_readtext_batch',side_effect=recognize):
            result=ocr.perform_video_ocr('fixture.mp4',srt_segments=segments,ocr_strategy='smart_skip',**kwargs)
        self.assertTrue(all(c.released for c in captures))
        return result,captures
    @staticmethod
    def rows(frames):
        return [[([[100,1152],[600,1152],[600,1240],[100,1240]],'这是当时导致伤口感染',.95)] for f in frames]
    def test_smart_mode_samples_every_cue(self):
        segs=[segment(i) for i in range(1,20)]
        _,caps=self.run_ocr(segs,self.rows)
        self.assertEqual(len(caps[0].timestamps),19)
        self.assertTrue(all(s.ocr_mask_status=='located' for s in segs))
    def test_batches_bounded_to_twelve(self):
        calls=[]
        def recognize(frames):calls.append(len(frames));return self.rows(frames)
        self.run_ocr([segment(i) for i in range(1,28)],recognize)
        self.assertLessEqual(max(calls),12)
    def test_rescue_missing_only(self):
        calls=[]
        def recognize(frames):
            calls.append(len(frames))
            if len(calls)==1:return [self.rows([frames[0]])[0],[]]
            # Rescue is cropped: convert original Y back to crop-local pixels.
            return [[([[100,100],[600,100],[600,150],[100,150]],'这是当时导致伤口感染',.95)] for _ in frames]
        segs=[segment(1),segment(2)]
        _,caps=self.run_ocr(segs,recognize)
        self.assertEqual(calls,[2,2])
        self.assertEqual(len(caps[1].timestamps),2)
        self.assertTrue(all(s.ocr_mask_status=='located' for s in segs))
    def test_no_caption_video_not_failed(self):
        segs=[segment()]
        self.run_ocr(segs,lambda frames:[[] for _ in frames])
        self.assertEqual(segs[0].ocr_mask_status,'no_caption_evidence')
    def test_all_decode_failures_not_silent_success(self):
        with self.assertRaises(geo.OCRGeometryError):
            self.run_ocr([segment()],lambda frames:[[] for _ in frames],capture_args={'readable':False})
    def test_report_saved_before_unresolved_error(self):
        calls=[]
        def recognize(frames):
            calls.append(len(frames))
            if len(calls)==1:
                return [self.rows([frames[0]])[0],
                        [([[100,1152],[600,1152],[600,1240],[100,1240]],'完全不同字幕',.95)]]
            return [[([[100,100],[600,100],[600,150],[100,150]],'完全不同字幕',.95)] for _ in frames]
        with tempfile.TemporaryDirectory() as tmp:
            report=Path(tmp)/'audit.json'
            with self.assertRaises(geo.OCRGeometryError):
                self.run_ocr([segment(),segment(2)],recognize,geometry_report_path=str(report))
            counts=json.loads(report.read_text())['geometry']['counts']
            self.assertEqual(counts['unresolved'],1)
    def test_gpu_failure_propagated(self):
        with self.assertRaisesRegex(RuntimeError,'GPU unavailable'):
            self.run_ocr([segment()],lambda _:(_ for _ in ()).throw(RuntimeError('GPU unavailable')))
    def test_short_full_mode_kept(self):
        calls=[]
        def recognize(frames):calls.append(len(frames));return self.rows(frames)
        self.run_ocr([segment()],recognize,capture_args={'duration':20.})
        self.assertEqual(sum(calls),2)


if __name__=='__main__':unittest.main(verbosity=2)
