import subprocess
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path

from backend.pipeline_v2.segments import RuntimeSegment
from backend.pipeline_v2.timing import (
    TimingPolicy,
    fit_audio_to_window,
    plan_actual_timing_rewrites,
    plan_segment,
    solve_segment_timing,
)


class TimingSolverTests(unittest.TestCase):
    def test_default_policy_matches_production_speed_envelope(self):
        policy = TimingPolicy()
        self.assertEqual(policy.atempo_min, 1.00)
        self.assertEqual(policy.atempo_max, 1.50)

    def test_budgeted_rewrite_runs_before_tts(self):
        segment = RuntimeSegment(
            index=1,
            start=timedelta(seconds=0),
            end=timedelta(seconds=1),
            content="Đây là một câu tiếng Việt rất dài và chắc chắn không thể đọc kịp",
            source_segment_id=7,
        )

        def rewrite(requests):
            self.assertEqual(requests[0].source_segment_id, 7)
            return {1: "Câu này quá dài"}

        solved = solve_segment_timing([segment], rewrite_callback=rewrite)
        self.assertEqual(solved.rewrite_rounds, 1)
        self.assertEqual(solved.segments[0].content, "Câu này quá dài")
        self.assertEqual(solved.segments[0].source_segment_id, 7)
        self.assertTrue(solved.plans[0].fits)

    def test_split_preserves_source_segment_id(self):
        segment = RuntimeSegment(
            index=4,
            start=timedelta(seconds=0),
            end=timedelta(seconds=1),
            content="Một câu rất dài, và phần thứ hai cũng dài, phần cuối vẫn dài.",
            source_segment_id=4,
        )
        solved = solve_segment_timing([segment])
        self.assertGreater(len(solved.segments), 1)
        self.assertEqual(
            {item.source_segment_id for item in solved.segments}, {4}
        )

    def test_split_never_emits_punctuation_only_tts_segments(self):
        segment = RuntimeSegment(
            index=26,
            start=timedelta(seconds=100.91),
            end=timedelta(seconds=102.09),
            content="Nếu... nếu ba...",
            source_segment_id=26,
        )
        solved = solve_segment_timing([segment])
        self.assertEqual(len(solved.segments), 1)
        self.assertTrue(
            all(
                any(character.isalnum() for character in item.content)
                for item in solved.segments
            )
        )
        self.assertEqual(
            "".join(item.content for item in solved.segments).replace(" ", ""),
            "Nếunếuba",
        )

    def test_plan_requires_only_light_atempo(self):
        segment = RuntimeSegment(
            index=1,
            start=timedelta(seconds=0),
            end=timedelta(seconds=2),
            content="Một câu vừa đủ",
        )
        self.assertLessEqual(plan_segment(segment).required_atempo, 1.08)

    def test_measured_duration_creates_a_smaller_second_pass_budget(self):
        segment = RuntimeSegment(
            index=3,
            start=timedelta(seconds=0),
            end=timedelta(seconds=1),
            content="Một câu lồng tiếng thực tế đang bị dài",
            source_segment_id=9,
        )
        requests = plan_actual_timing_rewrites(
            [segment],
            [
                {
                    "index": 3,
                    "actual_audio_duration": 1.5,
                    "timing_fits": False,
                }
            ],
        )
        self.assertEqual(len(requests), 1)
        self.assertEqual(requests[0].source_segment_id, 9)
        self.assertLess(requests[0].max_characters, len(segment.content.replace(" ", "")))

    def test_split_merges_short_introductory_clauses(self):
        segment = RuntimeSegment(
            index=146,
            start=timedelta(seconds=585.04),
            end=timedelta(seconds=587.76),
            content="Sau này, tôi đã học được cách sử dụng vỏ trái cây để bảo vệ những con rồng non.",
            source_segment_id=146,
        )
        solved = solve_segment_timing([segment])
        # Short clause "Sau này," (< 4 words) must not be isolated into a 0.3s micro-segment
        # Instead, it splits into balanced clauses where each clause has at least 4 words
        for s in solved.segments:
            self.assertGreaterEqual(len(s.content.split()), 4)
            dur = (s.end - s.start).total_seconds()
            self.assertGreaterEqual(dur, 1.0)
        self.assertEqual(
            "".join(s.content for s in solved.segments).replace(" ", ""),
            segment.content.replace(" ", ""),
        )

    def test_split_merges_short_phrases_in_compound_sentences(self):
        segment = RuntimeSegment(
            index=154,
            start=timedelta(seconds=618.34),
            end=timedelta(seconds=622.48),
            content="Assassin, tuy nhiên, đi một mình đến một con mương khác, và một con rồng miệng lạ đi theo anh ta, giữ lại nhiều hơn.",
            source_segment_id=154,
        )
        solved = solve_segment_timing([segment])
        # Must not create 1-2 word fragments like "Assassin," or "tuy nhiên,"
        for s in solved.segments:
            self.assertGreaterEqual(len(s.content.split()), 4)



class AudioFitIntegrationTests(unittest.TestCase):
    def test_short_audio_is_not_slowed_below_normal_speed(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.wav"
            output = root / "fitted.wav"
            subprocess.run(
                [
                    "ffmpeg",
                    "-hide_banner",
                    "-loglevel",
                    "error",
                    "-f",
                    "lavfi",
                    "-i",
                    "sine=frequency=440:duration=1",
                    str(source),
                ],
                check=True,
            )
            result = fit_audio_to_window(
                source,
                output,
                target_seconds=2.0,
            )
            self.assertEqual(result.applied_atempo, 1.00)
            self.assertTrue(result.fits)
            self.assertTrue(output.is_file())

    def test_atempo_is_capped_at_one_point_zero_eight(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.wav"
            output = root / "fitted.wav"
            subprocess.run(
                [
                    "ffmpeg",
                    "-hide_banner",
                    "-loglevel",
                    "error",
                    "-f",
                    "lavfi",
                    "-i",
                    "sine=frequency=440:duration=2",
                    str(source),
                ],
                check=True,
            )
            result = fit_audio_to_window(
                source,
                output,
                target_seconds=1.0,
                policy=TimingPolicy(atempo_max=1.08),
            )
            self.assertAlmostEqual(result.applied_atempo, 1.08, places=4)
            self.assertFalse(result.fits)
            self.assertTrue(output.is_file())


class GeminiCircuitBreakerTests(unittest.TestCase):
    def test_gemini_timing_rewriter_bypasses_when_gemini_unavailable(self):
        from unittest import mock
        from backend.pipeline_v2.timing import GeminiTimingRewriter, RewriteRequest

        rewriter = GeminiTimingRewriter(api_key="fake-key", models=["gemini-2.5-flash"])
        req = RewriteRequest(segment_index=1, text="Câu quá dài", max_characters=10, target_seconds=1.0, source_segment_id=1)

        with mock.patch("backend.pipeline_v2.timing._check_gemini_available", return_value=False):
            with mock.patch("requests.post") as mock_post:
                res = rewriter([req])
                self.assertEqual(res, {})
                mock_post.assert_not_called()

    def test_gemini_timing_rewriter_trips_circuit_breaker_on_429(self):
        from unittest import mock
        from backend.pipeline_v2.timing import GeminiTimingRewriter, RewriteRequest

        rewriter = GeminiTimingRewriter(api_key="fake-key", models=["model-1", "model-2"])
        req = RewriteRequest(segment_index=1, text="Câu quá dài", max_characters=10, target_seconds=1.0, source_segment_id=1)

        mock_resp = mock.Mock()
        mock_resp.status_code = 429

        with mock.patch("backend.pipeline_v2.timing._check_gemini_available", return_value=True):
            with mock.patch("backend.pipeline_v2.timing._mark_gemini_cooldown") as mock_cooldown:
                with mock.patch("requests.post", return_value=mock_resp) as mock_post:
                    res = rewriter([req])
                    self.assertEqual(res, {})
                    mock_cooldown.assert_called_once_with(180.0)
                    # Must break immediately and NOT try model-2!
                    self.assertEqual(mock_post.call_count, 1)


if __name__ == "__main__":
    unittest.main()
