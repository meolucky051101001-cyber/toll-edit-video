import asyncio
import unittest
from unittest.mock import AsyncMock, patch

from backend.pipeline_v2.config import PipelineSettings, QCGatePolicy
from backend.pipeline_v2.parallel import run_ocr_and_translation


class TestStage3QCPolicyAndConcurrency(unittest.TestCase):
    def test_parallel_run_ocr_and_translation_cleans_up_on_failure(self):
        """When one task fails, the other is properly cancelled and drained without orphaned tasks."""
        ocr_started = asyncio.Event()
        ocr_cancelled = False

        async def failing_ocr():
            ocr_started.set()
            await asyncio.sleep(0.01)
            raise RuntimeError("OCR hardware error")

        async def slow_translation():
            nonlocal ocr_cancelled
            try:
                await asyncio.sleep(10.0)
                return "translated"
            except asyncio.CancelledError:
                ocr_cancelled = True
                raise

        async def run_test():
            with self.assertRaises(RuntimeError) as ctx:
                await run_ocr_and_translation(failing_ocr, slow_translation, enabled=True)
            self.assertIn("OCR hardware error", str(ctx.exception))
            self.assertTrue(ocr_cancelled)

        asyncio.run(run_test())

    def test_parallel_run_ocr_and_translation_cleans_up_on_cancellation(self):
        """When outer coroutine is cancelled, both child tasks are drained cleanly."""
        t1_cancelled = False
        t2_cancelled = False

        async def slow_ocr():
            nonlocal t1_cancelled
            try:
                await asyncio.sleep(10.0)
            except asyncio.CancelledError:
                t1_cancelled = True
                raise

        async def slow_trans():
            nonlocal t2_cancelled
            try:
                await asyncio.sleep(10.0)
            except asyncio.CancelledError:
                t2_cancelled = True
                raise

        async def run_test():
            task = asyncio.create_task(run_ocr_and_translation(slow_ocr, slow_trans, enabled=True))
            await asyncio.sleep(0.02)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
            self.assertTrue(t1_cancelled)
            self.assertTrue(t2_cancelled)

        asyncio.run(run_test())

    def test_batch_processor_preserves_block_policy(self):
        """Batch processor must not override qc_gate_policy to WARN."""
        import inspect
        import ast
        import textwrap
        from backend import batch_processor
        pending = [batch_processor.process_single_local_video]
        visited = set()
        sources = []
        while pending:
            function = pending.pop()
            if function.__name__ in visited:
                continue
            visited.add(function.__name__)
            source = inspect.getsource(function)
            sources.append(source)
            for node in ast.walk(ast.parse(textwrap.dedent(source))):
                if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                    target = getattr(batch_processor, node.func.id, None)
                    if inspect.isfunction(target) and target.__module__ == batch_processor.__name__:
                        pending.append(target)
        source = "\n".join(sources)
        # Ensure hardcoded WARN replacement is gone
        self.assertNotIn("qc_gate_policy=QCGatePolicy.WARN", source)
        self.assertNotIn("qc_gate_policy = QCGatePolicy.WARN", source)


if __name__ == "__main__":
    unittest.main()
