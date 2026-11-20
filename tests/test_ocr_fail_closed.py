import ast
import unittest
from pathlib import Path

class OCRFailureTests(unittest.TestCase):
    def test_ocr_stage_records_failure_before_reraise(self):
        source = (Path(__file__).resolve().parents[1] / 'backend/v1_orchestrator.py').read_text(encoding='utf-8-sig')
        tree = ast.parse(source)
        stage = next(
            node for node in ast.walk(tree)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == '_stage_ocr'
        )
        handlers = []
        for node in ast.walk(stage):
            if not isinstance(node, ast.ExceptHandler):
                continue
            calls = [
                child for child in ast.walk(node)
                if isinstance(child, ast.Call)
                and isinstance(child.func, ast.Attribute)
                and child.func.attr == 'record_stage_failure'
            ]
            if calls:
                handlers.append((node, calls))
        self.assertEqual(len(handlers), 1)
        handler, calls = handlers[0]
        self.assertIsInstance(handler.type, ast.Name)
        self.assertEqual(handler.type.id, 'Exception')
        self.assertEqual(handler.name, 'exc')
        self.assertEqual([ast.unparse(arg) for arg in calls[0].args], ['c.job_id', 'stage_id', 'str(exc)'])
        self.assertIsInstance(handler.body[-1], ast.Raise)
        self.assertIsNone(handler.body[-1].exc)
        self.assertLess(calls[0].lineno, handler.body[-1].lineno)

    def test_ocr_geometry_error_is_not_retried(self):
        from v1_ocr_geometry import OCRGeometryError
        from v1_retry_policy import should_retry_job
        self.assertFalse(
            should_retry_job(OCRGeometryError("OCR mask coordinates unresolved"))
        )

    def test_worker_rejects_cpu_only_session(self):
        import importlib.util
        from unittest.mock import patch
        from types import SimpleNamespace
        path = Path(__file__).resolve().parents[1]/'backend/model_workers/v1_ocr_worker.py'
        spec = importlib.util.spec_from_file_location('ocr_guard_test', path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        class FakeSession:
            def __init__(self, *args, **kwargs): pass
            def disable_fallback(self): pass
            def get_providers(self): return ['CPUExecutionProvider']
        fake = SimpleNamespace(InferenceSession=FakeSession, preload_dlls=lambda **kw: None)
        with patch.dict('sys.modules', {'onnxruntime':fake}):
            module._configure_cuda()
            with self.assertRaisesRegex(RuntimeError, 'refusing CPU'):
                fake.InferenceSession('test')
