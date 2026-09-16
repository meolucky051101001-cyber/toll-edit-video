import tempfile
import unittest
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace

from backend.ai.transcription import _merge_short_fragments, _word_aligned_bounds
from backend.ass_utils import generate_ass_file
from backend.pipeline_v2.segments import RuntimeSegment


class WhisperTimingRegressionTests(unittest.TestCase):
    def test_word_bounds_replace_coarse_silence_spanning_bounds(self):
        segment = SimpleNamespace(
            start=0.18,
            end=13.46,
            words=[
                SimpleNamespace(start=11.78, end=12.26),
                SimpleNamespace(start=12.26, end=13.18),
            ],
        )

        self.assertEqual(_word_aligned_bounds(segment), (11.78, 13.18))

    def test_adjacent_complete_lines_are_not_merged(self):
        segments = [
            {"start": 23.68, "end": 24.98, "text": "line one"},
            {"start": 24.98, "end": 25.86, "text": "line two"},
        ]

        self.assertEqual(len(_merge_short_fragments(segments)), 2)

    def test_detached_alignment_token_does_not_span_long_silence(self):
        segment = SimpleNamespace(
            start=87.78,
            end=97.11,
            words=[
                SimpleNamespace(
                    start=87.78, end=88.46, word="阿", probability=0.998
                ),
                SimpleNamespace(
                    start=95.63, end=96.05, word="姨", probability=1.0
                ),
                SimpleNamespace(
                    start=96.05, end=97.11, word="都说没事了", probability=1.0
                ),
            ],
        )

        self.assertEqual(_word_aligned_bounds(segment), (95.63, 97.11))

    def test_tiny_adjacent_fragments_can_still_be_merged(self):
        segments = [
            {"start": 1.0, "end": 1.3, "text": "part one"},
            {"start": 1.32, "end": 1.7, "text": "part two"},
        ]

        merged = _merge_short_fragments(segments)
        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0]["text"], "part one part two")

    def test_detached_short_cluster_keeps_a_minimum_dubbing_window(self):
        segment = SimpleNamespace(
            start=157.12,
            end=163.4,
            text="我明白了",
            words=[
                SimpleNamespace(
                    start=157.12, end=157.98, word="我", probability=0.99
                ),
                SimpleNamespace(
                    start=163.0, end=163.22, word="明白", probability=1.0
                ),
                SimpleNamespace(
                    start=163.22, end=163.4, word="了", probability=1.0
                ),
            ],
        )

        start, end = _word_aligned_bounds(segment)
        self.assertAlmostEqual(end, 163.4)
        self.assertGreaterEqual(end - start, 0.65)


class SubtitleTimingRegressionTests(unittest.TestCase):
    def test_ass_does_not_extend_subtitle_across_silent_gap(self):
        segments = [
            RuntimeSegment(
                index=1,
                start=timedelta(seconds=1),
                end=timedelta(seconds=2),
                content="Cau mot",
            ),
            RuntimeSegment(
                index=2,
                start=timedelta(seconds=10),
                end=timedelta(seconds=11),
                content="Cau hai",
            ),
        ]
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "final.ass"
            generate_ass_file(segments, [], str(output))
            content = output.read_text(encoding="utf-8")

        self.assertIn("Dialogue: 1,0:00:01.00,0:00:02.00", content)
        self.assertNotIn("Dialogue: 1,0:00:01.00,0:00:10.00", content)


if __name__ == "__main__":
    unittest.main()
