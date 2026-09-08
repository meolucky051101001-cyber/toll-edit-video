import tempfile
import unittest
from unittest.mock import Mock,patch
from pathlib import Path
from backend.v1_ocr_proxy import ocr_proxy

class ProxyTests(unittest.TestCase):
    def test_large_source_preserves_original_dimensions(self):
        import cv2
        cap=Mock()
        cap.get.side_effect=lambda prop:2160 if prop==cv2.CAP_PROP_FRAME_WIDTH else 3840
        work=Mock(return_value=([],720,1280,.8))
        with patch.object(cv2,"VideoCapture",return_value=cap),patch("backend.v1_ocr_proxy.run") as run:
            result=ocr_proxy(work)("source.mp4")
        self.assertEqual(result,([],2160,3840,.8))
        self.assertEqual(run.call_count,1)
        self.assertNotEqual(work.call_args.args[0],"source.mp4")
        self.assertFalse(Path(work.call_args.args[0]).exists())

    def test_cancel_does_not_start_original_after_proxy(self):
        import cv2
        cap=Mock()
        cap.get.side_effect=lambda prop:2160 if prop==cv2.CAP_PROP_FRAME_WIDTH else 3840
        work=Mock()
        with patch.object(cv2,"VideoCapture",return_value=cap),patch("backend.v1_ocr_proxy.run",side_effect=RuntimeError("Batch stop requested")):
            with self.assertRaises(RuntimeError):
                ocr_proxy(work)("source.mp4")
        work.assert_not_called()
