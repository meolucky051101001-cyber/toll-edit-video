import tempfile
import unittest
from pathlib import Path
from unittest import mock

from backend.pipeline_v2.preflight import run_preflight


class PreflightTests(unittest.TestCase):
    def test_placeholder_token_is_reported_as_missing(self):
        with tempfile.TemporaryDirectory() as directory, mock.patch(
            "backend.pipeline_v2.preflight.importlib.util.find_spec",
            return_value=object(),
        ), mock.patch(
            "backend.pipeline_v2.preflight.shutil.which",
            return_value="tool.exe",
        ), mock.patch(
            "backend.pipeline_v2.preflight._nvenc_check"
        ) as nvenc, mock.patch(
            "backend.pipeline_v2.preflight._cuda_check"
        ) as cuda:
            from backend.pipeline_v2.preflight import PreflightCheck

            nvenc.return_value = PreflightCheck("encoder:h264_nvenc", "pass", "ok")
            cuda.return_value = PreflightCheck("gpu:cuda", "pass", "ok")
            report = run_preflight(
                Path(directory),
                "telegram",
                {
                    "BOT_TOKEN": "YOUR_TELEGRAM_BOT_TOKEN",
                    "AUTODUB_OUTPUT_DIR": str(Path(directory) / "output"),
                },
            )
        token = next(
            check for check in report["checks"] if check["name"] == "secret:BOT_TOKEN"
        )
        self.assertEqual(token["status"], "error")
        self.assertFalse(report["ready"])

    def test_api_wildcard_cors_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory, mock.patch(
            "backend.pipeline_v2.preflight.importlib.util.find_spec",
            return_value=object(),
        ), mock.patch(
            "backend.pipeline_v2.preflight.shutil.which",
            return_value="tool.exe",
        ), mock.patch(
            "backend.pipeline_v2.preflight._nvenc_check"
        ) as nvenc, mock.patch(
            "backend.pipeline_v2.preflight._cuda_check"
        ) as cuda:
            from backend.pipeline_v2.preflight import PreflightCheck

            nvenc.return_value = PreflightCheck("encoder:h264_nvenc", "pass", "ok")
            cuda.return_value = PreflightCheck("gpu:cuda", "pass", "ok")
            report = run_preflight(
                Path(directory),
                "api",
                {
                    "PIPELINE_MODE": "v2",
                    "AUTODUB_CORS_ORIGINS": "*",
                    "AUTODUB_WORKSPACE": str(Path(directory) / "workspace"),
                    "AUTODUB_OUTPUT_DIR": str(Path(directory) / "output"),
                },
            )
        cors = next(
            check for check in report["checks"] if check["name"] == "api:cors"
        )
        self.assertEqual(cors["status"], "error")
        self.assertFalse(report["ready"])


if __name__ == "__main__":
    unittest.main()
