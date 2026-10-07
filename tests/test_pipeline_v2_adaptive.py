import asyncio
import unittest

from backend.pipeline_v2.adaptive import (
    choose_output_dimensions,
    decide_demucs,
    decide_ocr_from_scores,
)
from backend.pipeline_v2.parallel import run_ocr_and_translation


class AdaptiveDecisionTests(unittest.TestCase):
    def test_explicit_clean_hint_skips_demucs(self):
        decision = decide_demucs("missing.wav", clean_audio_hint=True)
        self.assertFalse(decision.should_run)
        self.assertEqual(decision.confidence, 1.0)

    def test_uncertain_ocr_runs_and_clear_frames_skip(self):
        self.assertTrue(decide_ocr_from_scores([]).should_run)
        self.assertFalse(decide_ocr_from_scores([0.02] * 8).should_run)
        self.assertTrue(decide_ocr_from_scores([0.8, 0.7, 0.1, 0.1]).should_run)

    def test_resolution_policy_never_upscales(self):
        self.assertEqual(
            choose_output_dimensions(1280, 720, 1920, 1080, False),
            (1280, 720),
        )
        self.assertEqual(
            choose_output_dimensions(3840, 2160, 1920, 1080, False),
            (1920, 1080),
        )
        self.assertEqual(
            choose_output_dimensions(1280, 720, 640, 640, True),
            (1280, 720),
        )


class ParallelContextTests(unittest.IsolatedAsyncioTestCase):
    async def test_parallel_mode_starts_both_operations_before_release(self):
        started = set()
        both_started = asyncio.Event()
        release = asyncio.Event()

        async def task(name):
            started.add(name)
            if len(started) == 2:
                both_started.set()
            await release.wait()
            return name

        running = asyncio.create_task(
            run_ocr_and_translation(
                lambda: task("ocr"), lambda: task("translation"), True
            )
        )
        await asyncio.wait_for(both_started.wait(), timeout=1.0)
        release.set()
        result = await running
        self.assertTrue(result.ran_in_parallel)
        self.assertEqual({result.ocr, result.translation}, {"ocr", "translation"})


if __name__ == "__main__":
    unittest.main()


