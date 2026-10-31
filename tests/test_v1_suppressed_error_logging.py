import asyncio
import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch


PRIVATE_DETAILS = "api_key=fixture-not-a-key http://fixture-user:fixture-password@proxy.invalid"


class SuppressedErrorLoggingTests(unittest.TestCase):
    def assert_private_details_absent(self, records):
        self.assertTrue(records)
        for record in records:
            self.assertNotIn("fixture-not-a-key", record.getMessage())
            self.assertNotIn("fixture-password", record.getMessage())
            self.assertIs(record.exc_info, False)
            self.assertIsNone(record.stack_info)

    def test_glossary_read_failure_keeps_next_candidate_and_logs_without_payload(self):
        from backend.ai import translation

        broken = Mock()
        broken.is_file.return_value = True
        broken.read_text.side_effect = OSError(PRIVATE_DETAILS)
        fallback = Mock()
        fallback.is_file.return_value = True
        fallback.read_text.return_value = json.dumps({"entries": {"term": "translation"}})
        with patch.object(translation, "_glossary_paths", return_value=[broken, fallback]), \
             self.assertLogs(translation.logger, level="WARNING") as captured:
            result = translation.load_custom_glossary()
        self.assertEqual(result, {"term": "translation"})
        self.assertIn("load_custom_glossary", captured.records[0].getMessage())
        self.assertIn("OSError", captured.records[0].getMessage())
        self.assert_private_details_absent(captured.records)

    def test_tracker_directory_failure_remains_nonfatal_and_emits_warning(self):
        from backend import job_tracker

        workspace = Mock()
        workspace.mkdir.side_effect = PermissionError(PRIVATE_DETAILS)
        with patch.object(job_tracker, "WORKSPACE", workspace), \
             self.assertLogs(job_tracker.logger, level="WARNING") as captured:
            result = job_tracker._ensure_dir()
        self.assertIsNone(result)
        self.assertIn("PermissionError", captured.records[0].getMessage())
        self.assert_private_details_absent(captured.records)

    def test_cleanup_tracker_failure_is_warned_and_preserves_existing_call(self):
        from backend import main
        from backend import workspace_usage_service

        with patch.object(main.job_tracker, "get_status", side_effect=OSError(PRIVATE_DETAILS)), \
             patch.object(workspace_usage_service, "cleanup_jobs", return_value={"results": []}) as cleanup, \
             self.assertLogs(main.logger, level="WARNING") as captured:
            result = asyncio.run(main.api_workspace_cleanup({"jobs": ["fixture_job"], "mode": "full"}))
        self.assertEqual(result, {"results": []})
        # No filesystem deletion occurs: cleanup is a mock. The existing policy is reported separately.
        cleanup.assert_called_once_with(["fixture_job"], "full", [])
        self.assertIn("api_workspace_cleanup", captured.records[0].getMessage())
        self.assert_private_details_absent(captured.records)

    def test_duration_probe_failure_preserves_existing_size_estimate(self):
        from backend import segment_editor_service as editor

        with tempfile.TemporaryDirectory() as temporary:
            audio = Path(temporary) / "fixture.audio"
            audio.write_bytes(b"0" * 32000)
            with patch.object(editor, "_AUDIO_DURATION_CACHE", {}), \
                 patch.object(editor.subprocess, "run", side_effect=OSError(PRIVATE_DETAILS)), \
                 self.assertLogs(editor.logger, level="DEBUG") as captured:
                duration = editor.get_audio_duration_seconds(audio)
                cached = editor.get_audio_duration_seconds(audio)
            self.assertEqual(duration, 2.0)
            self.assertEqual(cached, duration)
        self.assertIn("get_audio_duration_seconds", captured.records[0].getMessage())
        self.assert_private_details_absent(captured.records)

    def test_lock_read_errors_are_logged_without_changing_existing_fallback(self):
        from backend import workflow_api as workflow
        import disk_guard
        import stop_service

        main_stub = SimpleNamespace(UNIFIED_PIPELINE_LOCK=SimpleNamespace(
            locked=Mock(side_effect=RuntimeError(PRIVATE_DETAILS))
        ))
        with patch.dict(sys.modules, {
                 "main": main_stub,
                 "shared_state": SimpleNamespace(WORKSPACE=Path("fixture_workspace")),
             }), \
             patch.object(workflow, "job_tracker", SimpleNamespace(
                 get_status=Mock(side_effect=RuntimeError(PRIVATE_DETAILS))
             ), create=True), \
             patch.object(stop_service, "is_stop_requested", return_value=False), \
             patch.object(disk_guard, "ensure_free_space") as space, \
             self.assertLogs(workflow.logger, level="WARNING") as captured:
            allowed = workflow._wait_pipeline_free("fixture_job", max_wait_s=1)
        # Phase 6 retains this existing fallback; its policy is listed for user review.
        self.assertTrue(allowed)
        space.assert_called_once()
        self.assertEqual(len(captured.records), 2)
        self.assertTrue(all("_wait_pipeline_free" in r.getMessage() for r in captured.records))
        self.assert_private_details_absent(captured.records)
