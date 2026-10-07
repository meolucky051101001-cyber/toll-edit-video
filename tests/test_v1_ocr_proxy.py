import tempfile
import unittest
from unittest.mock import Mock,patch
from pathlib import Path
import os
import sys
sys.path.insert(0, r"C:\tool v1\backend")
sys.path.insert(0, os.environ.get("V1_REPAIR_TEST_CODE", str(Path(__file__).resolve().parents[1] / "backend")))
import v1_ocr_proxy as proxy
from v1_media_streams import MainVideo

class ProxyTests(unittest.TestCase):
    def test_large_source_preserves_original_dimensions(self):
        source=MainVideo(1,2160,3840,30,2,2,1,True)
        work=Mock(return_value=([],720,1280,.8))
        with tempfile.TemporaryDirectory() as directory:
            original=Path(directory)/"source.mp4"
            original.write_bytes(b"original")
            def encode(command,**kwargs):
                Path(command[-1]).write_bytes(b"prepared")
            with patch.object(proxy,"probe_main_video",return_value=source),patch.object(proxy,"validate_video_output"),patch.object(proxy,"run",side_effect=encode) as run:
                result=proxy.ocr_proxy(work)(original)
            self.assertEqual(result,([],2160,3840,.8))
            self.assertEqual(run.call_count,1)
            self.assertNotEqual(work.call_args.args[0],str(original))
            self.assertTrue(Path(work.call_args.args[0]).exists())
            self.assertEqual(original.read_bytes(),b"original")

    def test_cancel_does_not_start_original_after_proxy(self):
        source=MainVideo(1,2160,3840,30,2,2,1,True)
        work=Mock()
        with tempfile.TemporaryDirectory() as directory:
            original=Path(directory)/"source.mp4"
            original.write_bytes(b"original")
            with patch.object(proxy,"probe_main_video",return_value=source),patch.object(proxy,"run",side_effect=RuntimeError("Batch stop requested")):
                with self.assertRaises(RuntimeError):
                    proxy.ocr_proxy(work)(original)
        work.assert_not_called()

if __name__ == "__main__":
    unittest.main()
