"""
test_v1_quality_gate.py - Bộ kiểm thử tự động cho Quality Gate 8 nhóm (Tool V1 - Giai đoạn 5).
Kiểm tra:
1. Xuất báo cáo QC đầy đủ 8 nhóm kiểm tra tự động.
2. Phát hiện lỗi CJK sót, câu rỗng, đè câu, clipping âm lượng.
3. Hoạt động của các chính sách REPORT_ONLY, WARN, BLOCK.
"""

import os
import sys
import unittest
import tempfile
import json
from pathlib import Path
from unittest.mock import patch

try:
    from backend.v1_quality_gate import (
        run_quality_gate,
        QCPolicy,
        QCCategory,
        QCReport,
        CriticalQualityError,
    )
    from backend import v1_quality_gate as qc_module
except ImportError:
    from v1_quality_gate import (
        run_quality_gate,
        QCPolicy,
        QCCategory,
        QCReport,
        CriticalQualityError,
    )
    import v1_quality_gate as qc_module


class DummySub:
    def __init__(self, index, content):
        self.index = index
        self.content = content


class QualityGateTests(unittest.TestCase):

    def test_confirmed_pixel_leak_remains_critical_for_report_only(self):
        leak = qc_module.QCCheckItem(
            QCCategory.SUBTITLE_STYLING.value, "visual_pixel_coverage", "FAIL",
            "Confirmed source-caption leak",
        )
        warning = qc_module.QCCheckItem(
            QCCategory.SUBTITLE_STYLING.value, "visual_pixel_coverage", "WARN",
            "Nearby scene text",
        )
        self.assertEqual(qc_module.critical_failures([leak, warning]), [leak])

    def test_invalid_ass_and_missing_tts_are_not_passed(self):
        """A mocked first frame does not prove valid subtitles or complete speech."""
        from unittest.mock import patch, MagicMock
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp = Path(tmp_dir)
            video = tmp / "output.mp4"
            video.write_bytes(b"dummy video data" * 1000)

            ass = tmp / "output.ass"
            ass.write_text("[Script Info]\nTitle: Test\n", encoding="utf-8")

            subs = [
                DummySub(1, "Xin chào các bạn"),
                DummySub(2, "Hôm nay chúng ta cùng tìm hiểu"),
            ]

            with patch("cv2.VideoCapture") as mock_cap_cls:
                mock_cap = MagicMock()
                mock_cap.isOpened.return_value = True
                mock_cap.get.side_effect = lambda prop: 120 if prop == 7 else (30.0 if prop == 5 else (1920 if prop == 3 else (1080 if prop == 4 else 0)))
                mock_cap.read.return_value = (True, "valid_frame")
                mock_cap_cls.return_value = mock_cap

                with self.assertRaises(RuntimeError):
                    run_quality_gate(
                        job_id="test_qc_01", final_video_path=video,
                        translated_subtitles=subs, ass_subtitle_path=ass,
                        policy="REPORT_ONLY", workspace_path=tmp,
                    )

            # File JSON được lưu trên đĩa
            json_file = tmp / "bot_system" / "qc_reports" / "test_qc_01.qc.json"
            self.assertTrue(json_file.is_file())
            report = json.loads(json_file.read_text(encoding="utf-8"))
            self.assertEqual(report["overall_status"], "FAILED")
            self.assertTrue(any(c["name"] == "tts_cue_coverage" and c["status"] == "FAIL" for c in report["checks"]))

    def test_qc_detect_cjk_and_empty_translation(self):
        """Phát hiện cảnh báo khi bản dịch sót chữ Hán hoặc rỗng."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp = Path(tmp_dir)
            video = tmp / "output.mp4"
            video.write_bytes(b"dummy video" * 1000)

            subs = [
                DummySub(1, "Đây là câu có chữ Hán 显卡"),
                DummySub(2, "   "),  # Rỗng
                DummySub(3, "Câu hợp lệ"),
            ]

            with self.assertRaises(RuntimeError):
                run_quality_gate(job_id="test_qc_cjk", final_video_path=video,
                    translated_subtitles=subs, policy="REPORT_ONLY", workspace_path=tmp)
            report = json.loads((tmp / "bot_system/qc_reports/test_qc_cjk.qc.json").read_text(encoding="utf-8"))
            cjk_check = next((c for c in report["checks"] if c["name"] == "no_cjk_residual"), None)
            self.assertIsNotNone(cjk_check)
            self.assertEqual(cjk_check["status"], "FAIL")

            empty_check = next((c for c in report["checks"] if c["name"] == "no_empty_subtitles"), None)
            self.assertIsNotNone(empty_check)
            self.assertEqual(empty_check["status"], "FAIL")

    def test_report_only_blocks_missing_output_but_keeps_report(self):
        """REPORT_ONLY is not permission to publish a missing output file."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp = Path(tmp_dir)
            fake_video = tmp / "non_existent.mp4"

            with self.assertRaises(RuntimeError):
                run_quality_gate(job_id="test_qc_fail", final_video_path=fake_video,
                    policy="REPORT_ONLY", workspace_path=tmp)
            report = json.loads((tmp / "bot_system/qc_reports/test_qc_fail.qc.json").read_text(encoding="utf-8"))
            self.assertEqual(report["overall_status"], "FAILED")
            self.assertGreater(report["failure_count"], 0)

    def test_qc_block_policy_raises_on_failure(self):
        """Chế độ BLOCK sẽ ném RuntimeError khi nghiệm thu thất bại."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp = Path(tmp_dir)
            fake_video = tmp / "non_existent.mp4"

            with self.assertRaises(RuntimeError):
                run_quality_gate(
                    job_id="test_qc_block",
                    final_video_path=fake_video,
                    policy="BLOCK",
                    workspace_path=tmp,
                )

    def test_block_policy_stops_failed_qc_and_report_only_does_not(self):
        """QC FAIL phải bị chặn ở cổng BLOCK; REPORT_ONLY chỉ ghi nhận lỗi."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp = Path(tmp_dir)
            corrupt = tmp / "corrupt.mp4"
            corrupt.write_bytes(os.urandom(32768))

            with self.assertRaises(CriticalQualityError):
                run_quality_gate(
                    job_id="test_block_critical",
                    final_video_path=corrupt,
                    policy="BLOCK",
                    workspace_path=tmp,
                )
            report = json.loads(
                (tmp / "bot_system" / "qc_reports" / "test_block_critical.qc.json").read_text(encoding="utf-8")
            )
            self.assertEqual(report["policy"], "BLOCK")
            self.assertEqual(report["overall_status"], "FAILED")
            self.assertGreater(report["failure_count"], 0)

            # Simulate a future non-critical FAIL to exercise the policy branch.
            with patch.object(qc_module, "critical_failures", return_value=[]):
                with self.assertRaisesRegex(RuntimeError, "Quality Gate BLOCK"):
                    qc_module.run_quality_gate(
                        job_id="test_block_branch",
                        final_video_path=corrupt,
                        policy="BLOCK",
                        workspace_path=tmp,
                    )
                report_only = qc_module.run_quality_gate(
                    job_id="test_report_only",
                    final_video_path=corrupt,
                    policy="REPORT_ONLY",
                    workspace_path=tmp,
                )
                self.assertEqual(report_only.overall_status, "FAILED")
                self.assertEqual(report_only.policy, "REPORT_ONLY")

    def test_resolution_and_fps_expansion(self):
        """Nới rộng độ phân giải và FPS: Chấp nhận mọi video 4K, 2K, 60fps, 120fps, 9:16,..."""
        from v1_media_streams import MainVideo
        from unittest.mock import patch

        cases = [
            (1440, 1080, 30.0, 1080, 1920, 30.0, "PASS"),
            (3840, 2160, 60.0, 1080, 1920, 30.0, "PASS"),
            (2560, 1440, 60.0, 1080, 1920, 30.0, "PASS"),
            (1920, 1080, 24.0, 1080, 1920, 30.0, "PASS"),
            (1080, 1920, 30.0, 1080, 1920, 30.0, "PASS"),
        ]

        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp = Path(tmp_dir)
            video = tmp / "output.mp4"
            video.write_bytes(b"dummy video content" * 1000)
            src_video = tmp / "source.mp4"
            src_video.write_bytes(b"dummy source content" * 1000)

            audio_file = tmp / "mixed.wav"
            audio_file.write_bytes(b"RIFF" + b"\x00" * 100)

            for sw, sh, sfps, ow, oh, ofps, expected_status in cases:
                with patch("v1_media_streams.probe_main_video") as mock_probe:
                    mock_probe.side_effect = lambda p: (
                        MainVideo(0, ow, oh, ofps, 10.0, 1, 1, False)
                        if str(p) == str(video)
                        else MainVideo(1, sw, sh, sfps, 10.0, 1, 1, False)
                    )
                    with patch("soundfile.info") as mock_sf, patch("cv2.VideoCapture") as mock_cap_cls:
                        mock_sf.return_value.duration = 10.0
                        mock_cap = unittest.mock.MagicMock()
                        mock_cap.isOpened.return_value = True
                        mock_cap.get.side_effect = lambda prop: 300 if prop == 7 else (ofps if prop == 5 else (ow if prop == 3 else (oh if prop == 4 else 0)))
                        mock_cap.read.return_value = (True, "frame")
                        mock_cap_cls.return_value = mock_cap

                        rep = run_quality_gate(
                            job_id=f"test_res_{sw}x{sh}",
                            final_video_path=video,
                            original_video_path=src_video,
                            mixed_audio_path=audio_file,
                            policy="REPORT_ONLY",
                            workspace_path=tmp,
                        )
                        geom_chk = next((c for c in rep.checks if c.name == "source_geometry_fps"), None)
                        self.assertIsNotNone(geom_chk)
                        self.assertEqual(geom_chk.status, expected_status)


if __name__ == "__main__":
    unittest.main()


class SequentialPixelQCTests(unittest.TestCase):
    """Use a real advancing decoder clock; never load an OCR model."""

    class Capture:
        def __init__(self, times, values=None):
            import numpy as np
            self.times = times
            self.images = [np.full((160, 240, 3), value, np.uint8)
                           for value in (values or range(len(times)))]
            self.index = 0
            self.read_calls = 0
            self.seek_calls = 0
            self.released = False

        def isOpened(self):
            return True

        def read(self):
            self.read_calls += 1
            if self.index >= len(self.images):
                return False, None
            image = self.images[self.index]
            self.index += 1
            return True, image

        def get(self, prop):
            import cv2
            return {cv2.CAP_PROP_FPS: 30.0,
                    cv2.CAP_PROP_FRAME_COUNT: len(self.times),
                    cv2.CAP_PROP_POS_MSEC: self.times[max(0, self.index - 1)] * 1000,
                    cv2.CAP_PROP_POS_FRAMES: self.index}.get(prop, 0)

        def set(self, *_):
            self.seek_calls += 1
            raise AssertionError("QC must not seek")

        def release(self):
            self.released = True

    def _run(self, capture, requests, *, duration=2.0, recognize=None, stop_check=None, tracking_text=None):
        from contextlib import nullcontext
        from types import SimpleNamespace
        import ocr_utils
        progress = []
        seen = []
        def inference(images):
            self.assertLessEqual(len(images), 8)
            seen.extend(int(image[0, 0, 0]) for image in images)
            return recognize(images) if recognize else [[] for image in images]
        block = (SimpleNamespace(start=0., end=duration, text=tracking_text)
                 if tracking_text is not None else object())
        segment = SimpleNamespace(index=1, start=0., end=.1, tracking_blocks=[block])
        with tempfile.TemporaryDirectory() as tmp_dir:
            video = Path(tmp_dir) / "render.mp4"
            video.write_bytes(b"fixture")
            with patch("cv2.VideoCapture", return_value=capture), \
                 patch.object(qc_module, "_transition_pixel_candidates", return_value=(requests, len(requests))), \
                 patch("v1_gpu_gatekeeper.gpu_gatekeeper", return_value=nullcontext()), \
                 patch.object(ocr_utils, "_readtext_batch", side_effect=inference), \
                 patch.object(ocr_utils, "release_ocr_reader"):
                result = qc_module.check_rendered_pixel_coverage(
                    video, [segment], rendered_duration_s=duration,
                    progress_callback=progress.append, stop_check=stop_check)
        return result, seen, progress

    @staticmethod
    def _request(time_s, box=(.2, .6, .8, .9)):
        return dict(index=1, mid_s=time_s, box=box, transition=True)

    def test_high_confidence_caption_leak_does_not_require_source_text_match(self):
        row = [[(30, 25), (80, 25), (80, 45), (30, 45)], "文字", .99]
        result, _, _ = self._run(self.Capture([0., .5, 1.]), [self._request(.5)],
                                tracking_text="中国", recognize=lambda images: [[row] for _ in images])
        self.assertEqual(result.status, "FAIL")
        self.assertEqual(result.details["confirmed_leak_count"], 1)
        self.assertEqual(qc_module.critical_failures([result]), [result])

    def test_vfr_requests_sorted_share_displayed_frame_without_seeks(self):
        capture = self.Capture([0., .04, .5, .9, 1.5])
        requests = [self._request(.7), self._request(.045), self._request(.045, (.3,.4,.7,.8)),
                    self._request(.9), self._request(1.75)]
        result, seen, progress = self._run(capture, requests)
        self.assertEqual(result.status, "PASS")
        self.assertEqual(seen, [1, 1, 2, 3, 4])
        self.assertEqual(capture.seek_calls, 0)
        self.assertEqual(capture.read_calls, 6)
        self.assertTrue(capture.released)
        self.assertEqual(result.details["unique_decoded_frames"], 4)
        self.assertEqual(result.details["decoded_sample_times_s"], [.04, .04, .5, .9, 1.5])
        self.assertEqual(result.details["sample_times_s"], [.045, .045, .7, .9, 1.75])
        self.assertEqual(progress[-1], dict(phase="ocr", completed=5, total=5))

    def test_midpoints_and_boundaries_have_one_forward_schedule(self):
        from types import SimpleNamespace
        from contextlib import nullcontext
        import ocr_utils
        capture = self.Capture([i/10 for i in range(20)])
        seg = SimpleNamespace(index=1, start=0., end=1., ocr_mask_status="located",
                              best_block=SimpleNamespace(x_pct=.2,max_x_pct=.8,y_pct=.6,max_y_pct=.9))
        with tempfile.TemporaryDirectory() as tmp_dir:
            video = Path(tmp_dir)/"fixture.mp4"; video.write_bytes(b"fixture")
            with patch("cv2.VideoCapture", return_value=capture), \
                 patch.object(qc_module, "_transition_pixel_candidates", return_value=([self._request(1.5)],1)), \
                 patch("v1_gpu_gatekeeper.gpu_gatekeeper", return_value=nullcontext()), \
                 patch.object(ocr_utils, "_readtext_batch", side_effect=lambda images: [[] for _ in images]), \
                 patch.object(ocr_utils,"release_ocr_reader"):
                result = qc_module.check_rendered_pixel_coverage(video,[seg],rendered_duration_s=2.)
        self.assertEqual(result.status,"PASS")
        self.assertEqual(result.details["sample_times_s"],[.5,1.5])
        self.assertEqual(capture.seek_calls,0)

    def test_decode_clock_errors_and_incomplete_ocr_never_pass(self):
        for times in ([0.,0.,.5], [0.,float("nan"),.5], [0.,.5,.4], [-1., .1, .2]):
            with self.subTest(times=times):
                result,_,_ = self._run(self.Capture(times),[self._request(.8)])
                self.assertEqual(result.status,"UNKNOWN")
        result,_,_ = self._run(self.Capture([0.,.5,1.]),[self._request(.5)],
                               recognize=lambda images: [])
        self.assertEqual(result.status,"UNKNOWN")

    def test_small_initial_b_frame_preroll_is_displayed_at_zero(self):
        capture = self.Capture([-1/30, 1/30, 2/30])
        result, seen, _ = self._run(capture, [self._request(0.), self._request(.04)])
        self.assertEqual(result.status, 'PASS')
        self.assertEqual(seen, [0, 1])
        self.assertEqual(result.details['decoded_sample_times_s'], [0., 1/30])
        self.assertTrue(result.details['initial_preroll_clamped'])

    def test_truncated_decoder_does_not_repeat_last_frame_until_duration(self):
        capture = self.Capture([0.,.1,.2,.3,.4])
        capture.images = capture.images[:2]
        result,_,_ = self._run(capture,[self._request(.4)],duration=.5)
        self.assertEqual(result.status,"UNKNOWN")

    def test_missing_last_frame_duration_does_not_extend_unverified_frame(self):
        import cv2
        cap = self.Capture([0.,.1,.3])
        stats=dict(decoded_frames=0,decode_seconds=0.)
        result=list(qc_module._sequential_qc_frames(
            cap,[self._request(.4)],cv2,stats,duration=None))
        self.assertEqual(result,[])

    def test_cancellation_propagates_and_releases_capture(self):
        capture = self.Capture([i/10 for i in range(20)])
        raised = [False]
        def stop():
            if capture.read_calls >= 3 and not raised[0]:
                raised[0] = True
                raise RuntimeError("requested cancellation")
        with self.assertRaisesRegex(RuntimeError,"requested cancellation"):
            self._run(capture,[self._request(1.5)],stop_check=stop)
        self.assertTrue(capture.released)

    def test_many_requests_keep_ocr_batches_bounded(self):
        capture = self.Capture([i/30 for i in range(60)])
        result,seen,_=self._run(capture,[self._request(i/30) for i in range(50)])
        self.assertEqual(result.status,"PASS")
        self.assertEqual(seen,list(range(50)))
        self.assertEqual(result.details["decoded_frames"],51)

    def test_real_vfr_decode_selects_displayed_frame_from_actual_pts(self):
        import shutil
        import subprocess
        import cv2
        import numpy as np
        ffmpeg,ffprobe=shutil.which("ffmpeg"),shutil.which("ffprobe")
        if not ffmpeg or not ffprobe:
            self.skipTest("FFmpeg and ffprobe required")
        with tempfile.TemporaryDirectory() as tmp_dir:
            root=Path(tmp_dir)
            raw=root/"source.bgr"
            raw.write_bytes(b"".join(np.full((64,64,3),i*25,np.uint8).tobytes() for i in range(8)))
            video=root/"vfr.mp4"
            subprocess.run([ffmpeg,"-v","error","-nostdin","-y","-f","rawvideo",
                "-pix_fmt","bgr24","-s","64x64","-r","25","-i",str(raw),
                "-vf",r"settb=1/1000,setpts=if(lt(N\,4)\,N*40\,160+(N-4)*300)",
                "-c:v","libx264","-qp","0","-bf","0","-threads","1",
                "-fps_mode","vfr",str(video)],check=True,capture_output=True,timeout=15)
            probe=subprocess.run([ffprobe,"-v","error","-show_format","-of","json",str(video)],
                                 check=True,capture_output=True,text=True,timeout=10)
            duration=float(json.loads(probe.stdout)["format"]["duration"])
            reference=[]
            cap=cv2.VideoCapture(str(video))
            try:
                while True:
                    ok,image=cap.read()
                    if not ok:
                        break
                    reference.append((cap.get(cv2.CAP_PROP_POS_MSEC)/1000,image))
            finally:
                cap.release()
            self.assertEqual(len(reference),8)
            self.assertGreater(reference[5][0]-reference[4][0],.2)
            requests=[self._request(pts+.001) for pts,_ in reversed(reference)]
            stats=dict(decoded_frames=0,decode_seconds=0.)
            cap=cv2.VideoCapture(str(video))
            try:
                actual=list(qc_module._sequential_qc_frames(cap,requests,cv2,stats,duration=duration))
            finally:
                cap.release()
            self.assertEqual(len(actual),len(reference))
            for (_,image,pts,_),(expected_pts,expected) in zip(actual,reference):
                self.assertEqual(pts,expected_pts)
                self.assertTrue(np.array_equal(image,expected))

