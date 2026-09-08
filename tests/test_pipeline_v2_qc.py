import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from backend.pipeline_v2.qc import (
    QCSettings,
    _check_ass_safe_area,
    _check_segments,
    _parse_srt_timestamp,
    run_report_only_qc,
)


class SegmentQcTests(unittest.TestCase):
    def test_srt_timestamp_parser(self):
        self.assertEqual(_parse_srt_timestamp("01:02:03,500"), 3723.5)

    def test_reports_missing_segment_id_and_audio(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "segments.json"
            path.write_text(
                json.dumps(
                    [
                        {"id": 1, "start": 0.0, "end": 1.0, "text": "Một"},
                        {
                            "id": 3,
                            "start": 1.0,
                            "end": 2.0,
                            "text": "Ba",
                            "audio_path": "missing.wav",
                        },
                    ]
                ),
                encoding="utf-8",
            )
            metrics, checks = _check_segments(path)
            self.assertEqual(metrics["missing_ids"], [2])
            self.assertEqual(metrics["missing_audio_count"], 1)
            self.assertEqual(checks[0].status, "error")

    def test_measured_timing_and_source_language_failures_are_blocking_checks(self):
        with tempfile.TemporaryDirectory() as directory:
            audio = Path(directory) / "voice.wav"
            audio.write_bytes(b"voice")
            path = Path(directory) / "segments.json"
            path.write_text(
                json.dumps(
                    {
                        "segments": [
                            {
                                "index": 1,
                                "start": 0.0,
                                "end": 1.0,
                                "content": "你好",
                                "orig_content": "你好",
                                "audio_path": str(audio),
                                "actual_audio_duration": 1.4,
                                "target_audio_duration": 1.0,
                                "timing_fits": False,
                            }
                        ]
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            metrics, checks = _check_segments(path)
            by_name = {check.name: check for check in checks}
            self.assertEqual(metrics["timing_failure_ids"], [1])
            self.assertEqual(by_name["segment_timing"].status, "error")
            self.assertEqual(by_name["translation_fallback"].status, "error")


class SubtitleQcTests(unittest.TestCase):
    def test_ass_position_outside_safe_area_is_reported(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "final.ass"
            path.write_text(
                "[Script Info]\nPlayResX: 720\nPlayResY: 1280\n"
                "[Events]\nDialogue: 0,0:00:00.00,0:00:01.00,TextStyle,,0,0,0,,"
                "{\\an8\\pos(360,1270)}Xin chào\n",
                encoding="utf-8",
            )
            metrics, checks = _check_ass_safe_area(path, QCSettings())
            self.assertEqual(metrics["outside_anchor_count"], 1)
            self.assertEqual(checks[0].status, "warning")


class ReportOnlyGuaranteeTests(unittest.TestCase):
    def test_missing_media_creates_atomic_non_blocking_report(self):
        with tempfile.TemporaryDirectory() as directory:
            report_path = Path(directory) / "qc_report.json"
            report = run_report_only_qc(
                video_path=Path(directory) / "missing.mp4",
                report_path=report_path,
                settings=QCSettings(sample_frames=False),
            )
            saved = json.loads(report_path.read_text(encoding="utf-8"))
            self.assertEqual(report.overall, "issues_found")
            self.assertFalse(saved["blocking"])
            self.assertTrue(saved["delivery_allowed"])
            self.assertGreaterEqual(saved["summary"]["error"], 1)

    @mock.patch("backend.pipeline_v2.qc._run_command")
    def test_findings_do_not_disable_delivery(self, run_command):
        probe_payload = {
            "format": {"duration": "2.0"},
            "streams": [
                {"codec_type": "video", "codec_name": "h264"},
                {"codec_type": "audio", "codec_name": "aac"},
            ],
        }
        loudness_log = "  I:         -20.0 LUFS\n  Peak:       -0.2 dBFS\n"
        silence_log = "silence_end: 2.0 | silence_duration: 2.0\n"
        run_command.side_effect = [
            mock.Mock(returncode=0, stdout=json.dumps(probe_payload), stderr=""),
            mock.Mock(returncode=0, stdout=json.dumps(probe_payload), stderr=""),
            mock.Mock(returncode=0, stdout="", stderr=loudness_log),
            mock.Mock(returncode=0, stdout="", stderr=silence_log),
        ]
        with tempfile.TemporaryDirectory() as directory:
            video = Path(directory) / "video.mp4"
            video.write_bytes(b"placeholder")
            report = run_report_only_qc(
                video_path=video,
                report_path=Path(directory) / "qc.json",
                settings=QCSettings(sample_frames=False),
            )
            self.assertEqual(report.overall, "issues_found")
            self.assertFalse(report.blocking)
            self.assertTrue(report.delivery_allowed)


if __name__ == "__main__":
    unittest.main()
