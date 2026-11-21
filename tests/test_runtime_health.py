import contextlib
import io
import json
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import patch

from scripts import verify_runtime_health as health
from backend import v1_preflight as preflight


def report(count=26, ready=True):
    return {"ready": ready, "checks": [{"name": str(i), "status": "pass"} for i in range(count)]}


class RuntimeHealthTests(unittest.TestCase):
    def test_all_checks_pass_and_preflight_is_read_only(self):
        commands = []

        def process(command, root, deadline):
            commands.append(command)
            if "pip" in command:
                return 0, "No broken requirements found"
            if "--interface" in command:
                return 0, json.dumps(report())
            return 0, "[3, 12, 9]"

        def endpoint(url, deadline, **kwargs):
            return {"status": "idle", "active": False,
                    "vram": {"used_gb": 2, "total_gb": 6}, "ram": {"percent": 50}}

        with patch.object(health, "_process", side_effect=process), patch.object(health, "_get_json", side_effect=endpoint):
            results = health.verify(Path("."), Path(sys.executable))
        self.assertTrue(all(item["ok"] for item in results))
        command = next(command for command in commands if "--interface" in command)
        self.assertIn("--read-only", command)
        self.assertEqual(command[command.index("--interface") + 1], "all")
        self.assertIn("active=false", results[3]["detail"])
        self.assertIn('"used_gb": 2', results[3]["detail"])

    def test_wrong_python_broken_pip_and_non_pass_preflight_fail(self):
        def process(command, root, deadline):
            if "pip" in command:
                return 1, "SENSITIVE dependency output"
            if "--interface" in command:
                result = report()
                result["checks"][0]["status"] = "warning"
                return 0, json.dumps(result)
            return 0, "[3, 10, 11]"

        with patch.object(health, "_process", side_effect=process), \
             patch.object(health, "_get_json", return_value={"status": "idle", "active": False}):
            results = health.verify(Path("."), Path(sys.executable))
        self.assertFalse(any(item["ok"] for item in results[:3]))
        self.assertNotIn("SENSITIVE", str(results))

    def test_preflight_requires_nonempty_all_pass_checks(self):
        def is_ok(result):
            with patch.object(health, "_process", return_value=(0, json.dumps(result))):
                return health._preflight(Path("."), Path(sys.executable), time.monotonic() + 2)[0]

        self.assertTrue(is_ok(report()))

        non_pass = report(count=1)
        non_pass["checks"][0]["status"] = "warning"
        self.assertFalse(is_ok(non_pass))
        self.assertFalse(is_ok(report(count=0)))

    def test_active_job_is_reported_as_healthy_and_metrics_are_filtered(self):
        with patch.object(health, "_get_json", return_value={
            "status": "running", "active": True, "secret": "PRIVATE",
            "ram": {"percent": 60, "api_key": "PRIVATE"}, "vram": {"used_gb": 2, "token": "PRIVATE"},
        }):
            ok, detail = health._dashboard(8088, time.monotonic() + 2)
        self.assertTrue(ok)
        self.assertIn("active=true", detail)
        self.assertNotIn("PRIVATE", detail)

    def test_control_falls_back_to_tcp_only_for_missing_endpoint(self):
        missing = urllib.error.HTTPError("http://127.0.0.1/health", 404, "missing", {}, None)
        with patch.object(health, "_get_json", side_effect=missing), \
             patch.object(health.socket, "create_connection") as connect:
            ok, detail = health._control(8090, time.monotonic() + 2)
        self.assertTrue(ok)
        self.assertIn("TCP", detail)
        connect.assert_called_once()
        failure = urllib.error.HTTPError("http://127.0.0.1/health", 500, "failure", {}, None)
        with patch.object(health, "_get_json", side_effect=failure), \
             patch.object(health.socket, "create_connection") as connect:
            with self.assertRaises(urllib.error.HTTPError):
                health._control(8090, time.monotonic() + 2)
            connect.assert_not_called()

    def test_deadline_never_becomes_a_false_pass(self):
        def expired(*args, **kwargs):
            raise TimeoutError()

        with patch.object(health, "_process", side_effect=expired), \
             patch.object(health, "_get_json", side_effect=expired):
            started = time.monotonic()
            results = health.verify(Path("."), Path(sys.executable), timeout=0.1)
        self.assertFalse(any(item["ok"] for item in results))
        self.assertTrue(all("incomplete" in item["detail"] for item in results))
        self.assertLess(time.monotonic() - started, 1)

    def test_owned_subprocess_is_bounded_and_reaped(self):
        with tempfile.TemporaryDirectory() as directory:
            started = time.monotonic()
            with self.assertRaises(subprocess.TimeoutExpired):
                health._process([sys.executable, "-B", "-c", "import time; time.sleep(10)"],
                                Path(directory), time.monotonic() + 0.15)
            self.assertLess(time.monotonic() - started, 2)

    def test_cli_exit_status_and_default_production_interpreter(self):
        for ok in (True, False):
            with self.subTest(ok=ok), patch.object(health, "verify", return_value=[{"name": "fixture", "ok": ok, "detail": "done"}]) as verify:
                out = io.StringIO()
                with contextlib.redirect_stdout(out):
                    code = health.main(["--project-root", str(Path.cwd())])
                self.assertEqual(code, 0 if ok else 1)
                self.assertEqual(out.getvalue().splitlines()[-1].split()[0], "PASS" if ok else "FAIL")
                self.assertEqual(verify.call_args.args[1], Path.cwd() / "backend" / "venv" / "Scripts" / "python.exe")
                self.assertEqual(verify.call_args.args[3], 5.0)


class ReadOnlyPreflightTests(unittest.TestCase):
    def test_directory_checks_never_create_or_probe_write_in_read_only_mode(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with patch.object(Path, "mkdir", side_effect=AssertionError("write")), \
                 patch.object(Path, "unlink", side_effect=AssertionError("write")), \
                 patch.object(preflight.tempfile, "mkstemp", side_effect=AssertionError("write")):
                self.assertEqual(preflight._directory_check("existing", root, read_only=True)["status"], "pass")
                self.assertEqual(preflight._directory_check("missing", root / "absent", read_only=True)["status"], "error")
            self.assertFalse((root / "absent").exists())

    def test_read_only_preflight_still_requires_all_checks(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            env = {"PIPELINE_MODE": "legacy", "BOT_TOKEN": "fixture", "GEMINI_API_KEY": "fixture",
                   "AUTODUB_WORKSPACE": str(root), "AUTODUB_OUTPUT_DIR": str(root)}
            with patch.object(preflight, "_load_env", return_value=env), \
                 patch.object(preflight.importlib.util, "find_spec", return_value=object()), \
                 patch.object(preflight.shutil, "which", return_value="fixture.exe"), \
                 patch.object(preflight, "_ffmpeg_version_check", return_value=preflight._check("binary:ffmpeg_version", "pass", "fixture")), \
                 patch.object(preflight, "_cuda_check", return_value=preflight._check("gpu:cuda", "pass", "fixture")), \
                 patch.object(preflight, "_optional_ocr_check", return_value=preflight._check("ocr", "pass", "fixture")), \
                 patch.object(preflight.tempfile, "mkstemp", side_effect=AssertionError("write")):
                result = preflight.run_preflight(root, "all", read_only=True)
            self.assertIs(result["ready"], True)
            self.assertEqual(len(result["checks"]), 26)
            self.assertTrue(all(check["status"] == "pass" for check in result["checks"]))

    def test_ffmpeg_version_check_requires_version_seven_or_newer(self):
        cases = (
            ("ffmpeg version 6.1 Copyright fixture\n", "error", "6.1"),
            ("ffmpeg version 8.0.1-full_build-www.gyan.dev Copyright fixture\n", "pass", "8.0.1-full_build-www.gyan.dev"),
            ("ffmpeg version 9.0.2 Copyright fixture\n", "pass", "9.0.2"),
            ("ffmpeg version N-118000-gfixture Copyright fixture\n", "warning", "unrecognized"),
        )
        for stdout, expected_status, expected_message in cases:
            with self.subTest(stdout=stdout), patch.object(
                preflight.subprocess,
                "run",
                return_value=subprocess.CompletedProcess(["ffmpeg", "-version"], 0, stdout, ""),
            ) as run:
                check = preflight._ffmpeg_version_check()
            self.assertEqual(check["status"], expected_status)
            self.assertIn(expected_message, check["message"])
            run.assert_called_once()
            self.assertEqual(run.call_args.kwargs["timeout"], 10)
            self.assertIn("creationflags", run.call_args.kwargs)

    def test_ffmpeg_version_probe_failure_is_a_warning(self):
        with patch.object(preflight.subprocess, "run", side_effect=subprocess.TimeoutExpired("ffmpeg", 10)):
            check = preflight._ffmpeg_version_check()
        self.assertEqual(check["status"], "warning")

    def test_ocr_timeout_is_incomplete_in_bounded_mode_and_fallback_otherwise(self):
        with patch.object(Path, "is_file", return_value=True), \
             patch.object(preflight.subprocess, "run", side_effect=subprocess.TimeoutExpired("fixture", 0.1)):
            bounded = preflight._optional_ocr_check(Path("."), {}, timeout_seconds=0.1)
            normal = preflight._optional_ocr_check(Path("."), {})
        self.assertEqual(bounded["status"], "error")
        self.assertEqual(normal["status"], "warning")
