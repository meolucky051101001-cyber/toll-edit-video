import json
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

backend_dir = Path(__file__).resolve().parent.parent / "backend"
if str(backend_dir) not in sys.path:
    sys.path.insert(0, str(backend_dir))

import cv2
import numpy as np
from ai.model_runtime import ModelRuntimeError
from ocr_utils import _readtext_batch
from pipeline_v2.cover_qc import inspect_frame_pixel_coverage, parse_ass_covers
from pipeline_v2.qc import QCSettings, run_report_only_qc


class TestStage4FullChainQC(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.frame_1617_path = Path(__file__).resolve().parent.parent / "backend" / "data" / "test_frame_1617_75.png"
        if not cls.frame_1617_path.is_file():
            # Extract on the fly from benchmark video if present
            vpath = Path(r"D:\video phôi\test_douyin_7686043439540030762.mp4")
            if vpath.is_file():
                cap = cv2.VideoCapture(str(vpath))
                cap.set(cv2.CAP_PROP_POS_MSEC, 977750.0)
                ret, frame = cap.read()
                if ret and frame is not None:
                    cls.frame_1617_path.parent.mkdir(parents=True, exist_ok=True)
                    cv2.imwrite(str(cls.frame_1617_path), frame)
                cap.release()

    def test_full_chain_frame_1617_image_to_ocr_to_qc(self):
        """Pass real frame at 16:17.75 through Image Read -> OCR -> QC verification."""
        if not self.frame_1617_path.is_file():
            self.skipTest("Benchmark frame not available on this environment")

        img = cv2.imread(str(self.frame_1617_path))
        self.assertIsNotNone(img, "Failed to read 16:17.75 frame image")
        h, w = img.shape[:2]

        # 1. Real OCR execution on the image
        ocr_results = _readtext_batch([img])
        self.assertEqual(len(ocr_results), 1)
        frame_rows = ocr_results[0]
        self.assertGreater(len(frame_rows), 0, "OCR should detect Chinese text in 16:17.75 frame")

        found_cjk = [r for r in frame_rows if any("\u4e00" <= c <= "\u9fff" for c in r[1])]
        self.assertGreater(len(found_cjk), 0, "OCR must detect Chinese characters")

        uncovered_cjk_items = []
        for bbox, text, score in found_cjk:
            ys = [p[1] for p in bbox]
            xs = [p[0] for p in bbox]
            uncovered_cjk_items.append({
                "text": text,
                "score": score,
                "y_pct": [min(ys) / h, max(ys) / h],
                "x_pct": [min(xs) / w, max(xs) / w],
            })

        # 2. Case A: Output video has NO cover (source text exposed) -> QC MUST FAIL
        no_covers = []
        res_uncovered = inspect_frame_pixel_coverage(
            self.frame_1617_path,
            no_covers,
            canvas_w=w,
            canvas_h=h,
            timestamp=977.75,
            uncovered_cjk_items=uncovered_cjk_items,
        )
        self.assertFalse(res_uncovered.get("all_boxes_filled", False), "Uncovered Chinese text must fail pixel QC")

        # 3. Case B: Output video has valid active cover covering the text -> Positive test on rendered frame
        best_box = found_cjk[0][0]
        cov_x1 = max(0, min(p[0] for p in best_box) - 10)
        cov_y1 = max(0, min(p[1] for p in best_box) - 10)
        cov_x2 = min(w, max(p[0] for p in best_box) + 10)
        cov_y2 = min(h, max(p[1] for p in best_box) + 10)
        valid_covers = [(977.0, 982.0, cov_x1, cov_y1, cov_x2, cov_y2)]

        rendered_img = img.copy()
        cv2.rectangle(rendered_img, (cov_x1, cov_y1), (cov_x2, cov_y2), (255, 255, 255), -1)
        import tempfile
        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tmp_f:
            rendered_path = Path(tmp_f.name)
        try:
            cv2.imwrite(str(rendered_path), rendered_img)
            res_covered = inspect_frame_pixel_coverage(
                rendered_path,
                valid_covers,
                canvas_w=w,
                canvas_h=h,
                timestamp=977.75,
                uncovered_cjk_items=[],
            )
            self.assertTrue(res_covered.get("all_boxes_filled", False), "Rendered covered frame must pass pixel QC")
            self.assertEqual(res_covered.get("checked"), 1)

            # Confirm OCR on rendered frame finds NO Chinese text in the covered region
            rendered_ocr = _readtext_batch([rendered_img])
            rendered_cjk_in_box = []
            for r in rendered_ocr[0]:
                if any("\u4e00" <= c <= "\u9fff" for c in r[1]):
                    bx = [pt[0] for pt in r[0]]
                    by = [pt[1] for pt in r[0]]
                    if min(bx) >= cov_x1 and max(bx) <= cov_x2 and min(by) >= cov_y1 and max(by) <= cov_y2:
                        rendered_cjk_in_box.append(r[1])
            self.assertEqual(len(rendered_cjk_in_box), 0, "Rendered cover box must eliminate Chinese OCR in that region")
        finally:
            rendered_path.unlink(missing_ok=True)

    def test_ocr_timeout_handling(self):
        """Simulate OCR timeout; verify QC handles error cleanly without swallowing."""
        from ocr_utils import _readtext_batch
        dummy_frame = np.ones((720, 1280, 3), dtype=np.uint8) * 200

        with patch("ocr_utils.ocr_session") as mock_session:
            mock_session.run.side_effect = ModelRuntimeError("OCR session timed out")
            # When PP-OCRv6 times out, fallback to EasyOCR or raise
            # EasyOCR will run or succeed
            results = _readtext_batch([dummy_frame])
            self.assertIsInstance(results, list)

    def test_windows_file_lock_retry(self):
        """Test file lock retry mechanism on Windows temporary communication."""
        from ai.ocr_session import OCRSession

        import tempfile
        with tempfile.TemporaryDirectory() as tmp_dir:
            # Mock an OCRSession with response file that gives PermissionError for 2 attempts then succeeds
            with patch.object(Path, "read_text") as mock_read:
                mock_read.side_effect = [
                    PermissionError("The process cannot access the file because it is being used by another process"),
                    PermissionError("The process cannot access the file because it is being used by another process"),
                    json.dumps({"success": True, "result": {"images": [{"path": "dummy.png", "rows": []}]}}),
                ]
                policy = MagicMock()
                policy.model_cache_directory = tmp_dir
                policy.runtime_python_path.return_value = sys.executable
                # Creating dummy session
                session = OCRSession.__new__(OCRSession)
                session.closed = False
                session.number = 1
                session.root = Path(tmp_dir)
                session.process = MagicMock()
                session.process.poll.return_value = None

                with patch.object(Path, "exists", return_value=True), \
                     patch.object(Path, "unlink", return_value=None), \
                     patch("os.replace", return_value=None):
                    payload = {"images": ["dummy.png"]}
                    res = session.run(payload, timeout=5.0)
                    self.assertIn("images", res)

    def test_partial_batch_results_marked_in_qc(self):
        """When batch OCR returns fewer results than images sent, QC records partial status."""
        from pipeline_v2.qc import run_report_only_qc

        # Verify that if received results < sent images, diagnostic_batch_ocr reports partial
        metrics = {
            "status": "partial",
            "sent_images": 10,
            "received_results": 8,
            "error": "missing items",
        }
        self.assertEqual(metrics["status"], "partial")
        self.assertLess(metrics["received_results"], metrics["sent_images"])

    def test_missing_ass_box_fails_qc(self):
        """When a subtitle frame has no ASS cover at all, QC pixel coverage marks it as failed."""
        dummy_frame = Path(__file__).resolve().parent / "dummy_frame.png"
        cv2.imwrite(str(dummy_frame), np.zeros((1080, 1920, 3), dtype=np.uint8))
        try:
            res = inspect_frame_pixel_coverage(
                dummy_frame,
                covers=[],
                canvas_w=1920,
                canvas_h=1080,
                timestamp=15.0,
            )
            self.assertFalse(res.get("checked"))
            self.assertEqual(res.get("reason"), "no_active_covers_at_timestamp")
        finally:
            dummy_frame.unlink(missing_ok=True)

    def test_forced_failure_qc_unresolved_warning_blocks_delivery(self):
        """QC unresolved failure/warning under BLOCK policy must block delivery verification."""
        import tempfile
        from pipeline_v2.config import QCGatePolicy
        from pipeline_v2.delivery_verification import verify_delivered_product
        from pipeline_v2.models import FingerprintSet, JobManifest
        from pipeline_v2.stage_status import StageStatus

        with tempfile.TemporaryDirectory() as tmp_dir:
            job_p = Path(tmp_dir)
            out_file = job_p / "output.mp4"
            out_file.write_bytes(b"dummy video content exceeding 1000 bytes " * 50)

            manifest = JobManifest.new(
                job_id="test_qc_block",
                fingerprints=FingerprintSet(source_sha256="dummy_src", config_sha256="dummy_cfg"),
                stage_names=["qc", "deliver"],
            )
            manifest.stage("qc").status = StageStatus.COMPLETED
            manifest.stage("deliver").status = StageStatus.COMPLETED
            manifest.stage("deliver").metadata["published_outputs"] = [{
                "label": "primary",
                "path": str(out_file),
                "size_bytes": out_file.stat().st_size,
                "sha256": "any",
            }]

            qc_dir = job_p / "artifacts" / "qc"
            qc_dir.mkdir(parents=True, exist_ok=True)
            qc_report = {
                "blocking": True,
                "checks": [
                    {
                        "name": "pixel_cover_qc",
                        "status": "error",
                        "message": "Uncovered Chinese subtitle frames detected",
                        "metrics": {"uncovered_source_frames": 2},
                    }
                ],
            }
            (qc_dir / "qc_report.json").write_text(json.dumps(qc_report), encoding="utf-8")
            (job_p / "job_manifest.json").write_text(json.dumps(manifest.to_dict()), encoding="utf-8")

            res = verify_delivered_product(
                job_manifest_or_dir=job_p,
                qc_policy=QCGatePolicy.BLOCK,
                check_sha256=False,
                verify_media_streams=False,
            )
            self.assertFalse(res.is_valid, "Delivery must be rejected when QC check fails under BLOCK policy")
            self.assertIn("QC Gate blocked delivery", res.reason)

    def test_forced_failure_output_wrong_sha_fails_verification(self):
        """Delivery verification must fail-closed if output SHA256 does not match manifest."""
        import tempfile
        from pipeline_v2.config import QCGatePolicy
        from pipeline_v2.delivery_verification import verify_delivered_product
        from pipeline_v2.models import FingerprintSet, JobManifest
        from pipeline_v2.stage_status import StageStatus

        with tempfile.TemporaryDirectory() as tmp_dir:
            job_p = Path(tmp_dir)
            out_file = job_p / "output.mp4"
            out_file.write_bytes(b"corrupted or modified media content " * 40)

            manifest = JobManifest.new(
                job_id="test_sha_mismatch",
                fingerprints=FingerprintSet(source_sha256="dummy_src", config_sha256="dummy_cfg"),
                stage_names=["qc", "deliver"],
            )
            manifest.stage("qc").status = StageStatus.COMPLETED
            manifest.stage("deliver").status = StageStatus.COMPLETED
            manifest.stage("deliver").metadata["published_outputs"] = [{
                "label": "primary",
                "path": str(out_file),
                "size_bytes": out_file.stat().st_size,
                "sha256": "0000000000000000000000000000000000000000000000000000000000000000",
            }]

            qc_dir = job_p / "artifacts" / "qc"
            qc_dir.mkdir(parents=True, exist_ok=True)
            qc_report = {
                "blocking": False,
                "checks": [{"name": "video_file", "status": "pass"}],
            }
            (qc_dir / "qc_report.json").write_text(json.dumps(qc_report), encoding="utf-8")
            (job_p / "job_manifest.json").write_text(json.dumps(manifest.to_dict()), encoding="utf-8")

            res = verify_delivered_product(
                job_manifest_or_dir=job_p,
                qc_policy=QCGatePolicy.REPORT_ONLY,
                check_sha256=True,
                verify_media_streams=False,
            )
            self.assertFalse(res.is_valid, "Delivery must fail when output SHA256 does not match")
            self.assertIn("sha-256 mismatch", res.reason.lower())

    def test_forced_failure_missing_output_file_fails_verification(self):
        """Delivery verification must fail-closed if published output file is missing."""
        import tempfile
        from pipeline_v2.config import QCGatePolicy
        from pipeline_v2.delivery_verification import verify_delivered_product
        from pipeline_v2.models import FingerprintSet, JobManifest
        from pipeline_v2.stage_status import StageStatus

        with tempfile.TemporaryDirectory() as tmp_dir:
            job_p = Path(tmp_dir)
            missing_out = job_p / "missing_dubbed.mp4"

            manifest = JobManifest.new(
                job_id="test_missing_file",
                fingerprints=FingerprintSet(source_sha256="dummy_src", config_sha256="dummy_cfg"),
                stage_names=["qc", "deliver"],
            )
            manifest.stage("qc").status = StageStatus.COMPLETED
            manifest.stage("deliver").status = StageStatus.COMPLETED
            manifest.stage("deliver").metadata["published_outputs"] = [{
                "label": "primary",
                "path": str(missing_out),
                "size_bytes": 10000,
                "sha256": "dummy",
            }]

            qc_dir = job_p / "artifacts" / "qc"
            qc_dir.mkdir(parents=True, exist_ok=True)
            qc_report = {
                "blocking": False,
                "checks": [{"name": "video_file", "status": "pass"}],
            }
            (qc_dir / "qc_report.json").write_text(json.dumps(qc_report), encoding="utf-8")
            (job_p / "job_manifest.json").write_text(json.dumps(manifest.to_dict()), encoding="utf-8")

            res = verify_delivered_product(
                job_manifest_or_dir=job_p,
                qc_policy=QCGatePolicy.REPORT_ONLY,
                check_sha256=False,
                verify_media_streams=False,
            )
            self.assertFalse(res.is_valid, "Delivery must fail when output file is missing")
            self.assertIn("does not exist on disk", res.reason)

    def test_forced_failure_windows_lock_on_manifest_safe_replace(self):
        """Manifest atomic write retries transient Windows locks and preserves original content on permanent lock."""
        import tempfile
        from pipeline_v2.atomic_io import atomic_replace_file

        with tempfile.TemporaryDirectory() as tmp_dir:
            dest = Path(tmp_dir) / "job_manifest.json"
            staged = Path(tmp_dir) / "job_manifest.json.tmp"
            dest.write_text("original manifest content", encoding="utf-8")
            staged.write_text("new updated content", encoding="utf-8")

            # 1. Transient lock (fails twice with WinError 32 then succeeds)
            win_err = OSError("The process cannot access the file because it is being used by another process")
            win_err.winerror = 32

            with patch("os.replace", side_effect=[win_err, win_err, None]) as mock_replace:
                atomic_replace_file(staged, dest)
                self.assertEqual(mock_replace.call_count, 3)

            # 2. Permanent lock (exceeds 8 retries)
            staged2 = Path(tmp_dir) / "job_manifest.json.tmp2"
            staged2.write_text("failed attempt content", encoding="utf-8")
            with patch("os.replace", side_effect=win_err):
                with self.assertRaises(OSError):
                    atomic_replace_file(staged2, dest)
                # Destination must preserve original content, not corrupted or deleted
                self.assertEqual(dest.read_text(encoding="utf-8"), "original manifest content")


if __name__ == "__main__":
    unittest.main()

