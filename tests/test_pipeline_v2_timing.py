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
        self.assertEqual(policy.atempo_min, 0.92)
        self.assertEqual(policy.atempo_max, 1.40)

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

        self.assertGreater(len(solved.segments), 1)
        self.assertTrue(
            all(any(character.isalnum() for character in item.content) for item in solved.segments)
        )
        self.assertEqual(
            "".join(item.content for item in solved.segments).replace(" ", ""),
            segment.content.replace(" ", ""),
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


class AudioFitIntegrationTests(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
