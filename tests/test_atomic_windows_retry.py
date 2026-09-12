import ctypes
import os
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch
from backend.pipeline_v2.atomic_io import atomic_write_text


def locked(code=5):
    error = PermissionError('Windows file temporarily locked')
    error.winerror = code
    return error


class AtomicRetryTests(unittest.TestCase):
    def test_transient_windows_denial_retries_and_publishes(self):
        with tempfile.TemporaryDirectory() as root:
            target = Path(root) / 'job_manifest.json'
            target.write_text('old')
            real_replace = os.replace
            calls = []
            def replace(source, dest):
                calls.append(1)
                self.assertEqual(target.read_text(), 'old')
                if len(calls) < 3:
                    raise locked()
                return real_replace(source, dest)
            with patch('backend.pipeline_v2.atomic_io.os.replace', side_effect=replace), patch('backend.pipeline_v2.atomic_io.time.sleep'):
                atomic_write_text(target, 'new')
            self.assertEqual(target.read_text(), 'new')
            self.assertEqual(len(calls), 3)
            self.assertEqual(list(Path(root).iterdir()), [target])

    def test_permanent_denial_is_bounded_and_keeps_old_manifest(self):
        with tempfile.TemporaryDirectory() as root:
            target = Path(root) / 'job_manifest.json'
            target.write_text('old')
            with patch('backend.pipeline_v2.atomic_io.os.replace', side_effect=locked()) as replace, patch('backend.pipeline_v2.atomic_io.time.sleep'):
                with self.assertRaises(PermissionError):
                    atomic_write_text(target, 'new')
            self.assertEqual(replace.call_count, 8)
            self.assertEqual(target.read_text(), 'old')
            self.assertEqual(list(Path(root).iterdir()), [target])

    def test_other_io_errors_are_not_retried(self):
        with tempfile.TemporaryDirectory() as root:
            with patch('backend.pipeline_v2.atomic_io.os.replace', side_effect=OSError('disk error')) as replace:
                with self.assertRaises(OSError):
                    atomic_write_text(Path(root)/'manifest.json', 'new')
            self.assertEqual(replace.call_count, 1)

    @unittest.skipUnless(os.name == 'nt', 'Windows sharing semantics')
    def test_real_windows_reader_lock_is_retried_until_released(self):
        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        kernel.CreateFileW.argtypes = [ctypes.c_wchar_p, ctypes.c_ulong, ctypes.c_ulong, ctypes.c_void_p, ctypes.c_ulong, ctypes.c_ulong, ctypes.c_void_p]
        kernel.CreateFileW.restype = ctypes.c_void_p
        kernel.CloseHandle.argtypes = [ctypes.c_void_p]
        with tempfile.TemporaryDirectory() as root:
            target = Path(root)/'job_manifest.json'
            target.write_text('old')
            handle = kernel.CreateFileW(str(target), 0x80000000, 1, None, 3, 0, None)
            self.assertNotEqual(handle, ctypes.c_void_p(-1).value)
            timer = threading.Timer(.25, kernel.CloseHandle, args=(handle,))
            timer.start()
            try:
                atomic_write_text(target, 'new')
            finally:
                timer.join()
            self.assertEqual(target.read_text(), 'new')
