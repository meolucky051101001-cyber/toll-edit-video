"""Regressions found by the real 4K/VFR acceptance clip, not just synthetic E2E."""
import asyncio
from datetime import timedelta
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from backend.ass_utils import generate_ass_file, _held_tracking_blocks
from backend.pipeline_v2.segments import RuntimeSegment
from backend.pipeline_v2.qc import _sample_frames


class ReleaseRegressions(unittest.TestCase):
    def test_incomplete_frame_verification_blocks_strict_delivery(self):
        from backend.pipeline_v2.qc import QCCheck, QCSettings, run_report_only_qc, evaluate_qc_gate
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            video = root / 'input.mp4'
            video.write_bytes(b'mocked')
            with patch('backend.pipeline_v2.qc.probe_media', return_value={
                'format': {'duration': '2'}, 'streams': [{'codec_type': 'video', 'duration': '2'}]
            }), patch('backend.pipeline_v2.qc._sample_frames', return_value=([], [
                QCCheck('frame_samples', 'warning', 'missing frame')])):
                report = run_report_only_qc(video, root / 'qc.json', settings=QCSettings(gate_policy='block'))
            check = next(c for c in report.checks if c.name == 'frame_samples')
            self.assertEqual(check.status, 'error')
            self.assertIn('frame_samples', evaluate_qc_gate(report, 'block').blocking_checks)

    def test_pixel_qc_keeps_the_final_centisecond_of_an_active_cover(self):
        from PIL import Image
        from backend.pipeline_v2.cover_qc import inspect_frame_pixel_coverage
        with tempfile.TemporaryDirectory() as directory:
            image = Path(directory) / 'white.png'
            Image.new('RGB', (100, 100), (240, 240, 240)).save(image)
            covers = [(4.38, 4.42, 10, 10, 90, 90)]
            before = inspect_frame_pixel_coverage(image, covers, 100, 100, timestamp=4.410021)
            after = inspect_frame_pixel_coverage(image, covers, 100, 100, timestamp=4.42)
            self.assertTrue(before['checked'])
            self.assertTrue(before['all_boxes_filled'])
            self.assertFalse(after['checked'])

    def test_release_metadata_requires_real_successful_test_evidence(self):
        from scripts.generate_release_manifest import test_evidence
        self.assertEqual(test_evidence(None)['status'], 'NOT_RUN')
        with tempfile.TemporaryDirectory() as directory:
            log = Path(directory) / 'tests.log'
            for text in ('', 'Ran 300 tests in 1.0s\nFAILED (failures=1)\n', 'still running'):
                log.write_text(text, encoding='utf-8')
                with self.assertRaises(ValueError):
                    test_evidence(log)
            log.write_text('Ran 8 tests in 1.234s\n\nOK\n', encoding='utf-8')
            self.assertEqual(test_evidence(log)['status'], '8/8 PASS')

    def test_overlapping_asr_windows_do_not_turn_a_real_caption_into_packaging(self):
        from backend.ocr_subtitle_locator import select_chinese_subtitle_band
        from tests.test_subtitle_tracking_regression import row
        texts = {3: '因为这个展示牌是这种大开口的设计', 4: '比它大一圈也不影响放置',
                 5: '听大家的建议用丝带替换了挂绳'}
        blocks = [row(texts[3], t=8.8, sid=3, y=.69),
                  row(texts[4], t=9.7, sid=3, y=.69),
                  row(texts[4], t=10.8, sid=4, y=.69),
                  row(texts[4], t=12.0, sid=5, y=.69),
                  row(texts[5], t=12.4, sid=5, y=.69)]
        band = select_chinese_subtitle_band(blocks, texts, 720, 1280)
        self.assertIn(4, band.selected_by_segment)
        self.assertEqual(band.selected_by_segment[4]['text'], texts[4])

    def test_cover_stabilizes_nearby_boxes_without_unioning_a_vertical_jump(self):
        segment = RuntimeSegment(1, timedelta(0), timedelta(seconds=3), 'Xin chào')
        segment.tracking_blocks = [
            dict(start=0, end=1, x_pct=.15, max_x_pct=.85, y_pct=.69, max_y_pct=.72),
            dict(start=1, end=2, x_pct=.34, max_x_pct=.64, y_pct=.69, max_y_pct=.72),
            dict(start=2, end=3, x_pct=.25, max_x_pct=.75, y_pct=.30, max_y_pct=.34)]
        held = _held_tracking_blocks([segment], 4)
        self.assertEqual(held[1]['x_pct'], .15)
        self.assertEqual(held[1]['max_x_pct'], .85)
        self.assertEqual(held[2]['y_pct'], .30)
        self.assertLess(held[2]['max_y_pct'] - held[2]['y_pct'], .05)

    def test_ass_has_no_zero_length_events_at_fractional_transition(self):
        segment = RuntimeSegment(1, timedelta(0), timedelta(seconds=1), 'Xin chào')
        segment.tracking_blocks = [dict(start=a, end=b, x_pct=.2, max_x_pct=.8,
            y_pct=.69, max_y_pct=.72) for a, b in [(0, .5001), (.5001, .502), (.502, 1)]]
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / 'subs.ass'
            generate_ass_file([segment], [], output, video_duration=2)
            lines = [l.split(',', 9) for l in output.read_text(encoding='utf-8-sig').splitlines()
                     if l.startswith('Dialogue:')]
            self.assertTrue(lines)
            self.assertTrue(all(l[1] != l[2] for l in lines))

    def test_qc_uses_actual_vfr_pts_and_shares_one_frame_for_multiple_labels(self):
        def run(command, timeout):
            Path(command[-1].replace('%06d', '000000')).write_bytes(b'png')
            return Mock(returncode=0, stderr='[showinfo] n:0 pts:71 pts_time:0.071', stdout='')
        with tempfile.TemporaryDirectory() as directory, patch(
            'backend.pipeline_v2.qc.plan_diagnostic_samples', return_value=[('a', .04), ('b', .06)]
        ), patch('backend.pipeline_v2.qc._run_command', side_effect=run) as invoked:
            artifacts, checks = _sample_frames(Path('vfr.mp4'), 1, Path(directory), 'ffmpeg', 5)
        self.assertEqual(len(artifacts), 2)
        self.assertTrue(all(a['metadata']['timestamp_seconds'] == .071 for a in artifacts))
        self.assertEqual(checks[0].metrics['decoded_unique_frames'], 1)
        self.assertNotIn('fps=', invoked.call_args.args[0][10])

    def test_qc_does_not_invent_pts_when_decoder_does_not_report_them(self):
        with tempfile.TemporaryDirectory() as directory, patch(
            'backend.pipeline_v2.qc._run_command', return_value=Mock(returncode=0, stderr='', stdout='')
        ):
            artifacts, checks = _sample_frames(Path('vfr.mp4'), 1, Path(directory), 'ffmpeg', 5)
        self.assertEqual(artifacts, [])
        self.assertEqual(checks[0].status, 'warning')

    def test_gpu_preview_failure_never_retries_cpu(self):
        import cv2  # Initialize optional dependencies before mocking subprocess.
        from backend.video_sampling import sample_video_frames
        import subprocess
        with patch('backend.video_sampling.subprocess.run', side_effect=subprocess.TimeoutExpired('ffmpeg', 60)) as run:
            with self.assertRaises(subprocess.TimeoutExpired):
                sample_video_frames('video.mp4', end=2)
        self.assertEqual(run.call_count, 1)
        self.assertIn('cuda', run.call_args.args[0])


class ParallelTimingRegression(unittest.IsolatedAsyncioTestCase):
    async def test_fast_stage_is_checkpointed_before_slow_sibling_finishes(self):
        from tests.test_pipeline_v2_video_pipeline import FakeVideoPipelineRunner
        from backend.pipeline_v2.video_pipeline import VideoPipelineRequest
        from backend.pipeline_v2.config import PipelineSettings, PipelineMode
        from backend.pipeline_v2.models import StageStatus
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / 'input.mp4'
            source.write_bytes(b'video')
            runner = FakeVideoPipelineRunner(VideoPipelineRequest(
                video_path=source, job_directory=root / 'job', output_path=root / 'out.mp4',
                settings=PipelineSettings(mode=PipelineMode.V2)))
            runner.manifest = runner._load_or_create_manifest()
            finished_translation = asyncio.Event()
            async def translate(_):
                return [runner.artifact_store.put_bytes('translation/proof', b'yes')]
            async def ocr(_):
                await asyncio.wait_for(finished_translation.wait(), 2)
                self.assertIs(runner.manifest.stage('translate').status, StageStatus.COMPLETED)
                self.assertIs(runner.manifest.stage('ocr').status, StageStatus.RUNNING)
                return [runner.artifact_store.put_bytes('ocr/proof', b'yes')]
            async def notify(stage, state):
                if stage == 'translate' and state == 'completed':
                    finished_translation.set()
            runner._ocr_stage, runner._translate_stage, runner._notify = ocr, translate, notify
            await runner._execute_parallel_context([])
