import ast
import unittest
from pathlib import Path

class OCRFailureTests(unittest.TestCase):
    def test_both_telegram_ocr_handlers_raise(self):
        tree = ast.parse((Path(__file__).resolve().parents[1]/'backend/telegram_bot.py').read_text(encoding='utf-8-sig'))
        handlers = [n for n in ast.walk(tree) if isinstance(n, ast.ExceptHandler) and 'OCR Error:' in ast.unparse(n)]
        self.assertEqual(len(handlers), 2)
        for handler in handlers:
            self.assertIsInstance(handler.body[-1], ast.Raise)
            self.assertIsNotNone(handler.body[-1].cause)

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
