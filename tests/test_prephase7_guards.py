import asyncio
import copy
import json
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch


def idle_state():
    return {"active": False, "workers": {}, "video_name": "old.mp4", "updated_at": time.time()}


class StateGuardTests(unittest.TestCase):
    def test_corrupt_or_incomplete_status_never_falls_back_to_idle_memory(self):
        from backend import job_tracker as tracker
        from backend.v1_integrity_errors import JobStateReadError

        with tempfile.TemporaryDirectory() as folder:
            status = Path(folder) / "status.json"
            with patch.object(tracker, "STATUS_FILE", status), \
                 patch.object(tracker, "BATCH_LOCK_FILE", Path(folder) / "batch.lock"), \
                 patch.object(tracker, "_CURRENT_STATE", copy.deepcopy(tracker._DEFAULT_STATE)):
                for value in ('{broken', '[]', '{}', '{"active":"no","updated_at":1}'):
                    with self.subTest(value=value):
                        status.write_text(value)
                        with self.assertRaises(JobStateReadError):
                            tracker.get_status(strict=True)
                        self.assertFalse(tracker._CURRENT_STATE["active"])

    def test_live_batch_lock_with_idle_status_is_unknown_not_idle(self):
        from backend import job_tracker as tracker
        from backend.v1_integrity_errors import JobStateReadError

        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "status.json").write_text(json.dumps(idle_state()))
            (root / "batch.lock").write_text(json.dumps({"pid": 123, "job_id": "busy"}))
            with patch.object(tracker, "STATUS_FILE", root / "status.json"), \
                 patch.object(tracker, "BATCH_LOCK_FILE", root / "batch.lock"), \
                 patch.object(tracker, "_CURRENT_STATE", copy.deepcopy(tracker._DEFAULT_STATE)), \
                 patch.object(tracker, "_pid_is_running", return_value=True):
                with self.assertRaises(JobStateReadError):
                    tracker.get_status(strict=True)

    def test_active_names_ignore_finished_video_but_include_workers_and_job_ids(self):
        from backend import job_tracker as tracker
        from backend.v1_integrity_errors import JobStateReadError

        state = idle_state()
        self.assertEqual(tracker.active_job_names(state), [])
        state["workers"] = {"1": {"active": True, "video_name": "busy.mp4", "job_id": "job_busy"}}
        self.assertEqual(tracker.active_job_names(state), ["busy.mp4", "job_busy"])
        state["workers"]["1"]["video_name"] = ""
        with self.assertRaises(JobStateReadError):
            tracker.active_job_names(state)

    def test_cleanup_protects_active_job_and_can_remove_finished_temp_job(self):
        from backend import main, workspace_usage_service as usage

        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            job = root / "busy"
            job.mkdir()
            source = job / "fixture.txt"
            source.write_text("temporary fixture")
            active = {**idle_state(), "active": True, "video_name": "busy.mp4"}
            with patch.object(usage, "_workspace_roots", return_value=[root]), \
                 patch.object(main.job_tracker, "get_status", return_value=active):
                result = asyncio.run(main.api_workspace_cleanup({"jobs": ["busy"], "mode": "full"}))
                self.assertEqual(result["results"][0]["status"], "rejected")
                self.assertTrue(source.is_file())
            with patch.object(usage, "_workspace_roots", return_value=[root]), \
                 patch.object(main.job_tracker, "get_status", return_value=idle_state()):
                result = asyncio.run(main.api_workspace_cleanup({"jobs": ["busy"], "mode": "full"}))
                self.assertEqual(result["results"][0]["status"], "ok")
                self.assertFalse(job.exists())

    def test_cleanup_reports_failed_removal_instead_of_success(self):
        from backend import workspace_usage_service as usage

        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "fixture").mkdir()
            with patch.object(usage, "_workspace_roots", return_value=[root]), \
                 patch.object(usage.shutil, "rmtree", side_effect=PermissionError("fixture")):
                result = usage.cleanup_jobs(["fixture"], "full")
            self.assertEqual(result["results"][0]["status"], "error")
            self.assertEqual(result["total_freed_mb"], 0)
            self.assertTrue((root / "fixture").exists())

    def test_segment_edit_guard_blocks_unknown_tracker_state(self):
        from backend import phase_c_routes as routes
        import job_tracker

        with patch.object(job_tracker, "get_status", side_effect=OSError("fixture")):
            with self.assertRaises(routes.HTTPException) as caught:
                routes._job_is_being_processed("fixture")
        self.assertEqual(caught.exception.status_code, 503)


class WorkflowGuardTests(unittest.TestCase):
    def test_busy_pipeline_timeout_does_not_touch_stop_state_or_disk(self):
        import sys
        from backend import workflow_api as workflow
        import stop_service
        import disk_guard

        clock = SimpleNamespace(monotonic=Mock(side_effect=[0, 0, 0, 2]), sleep=Mock())
        main = SimpleNamespace(UNIFIED_PIPELINE_LOCK=SimpleNamespace(locked=lambda: True))
        with patch.dict(sys.modules, {"main": main}), \
             patch.object(workflow, "time", clock), \
             patch.object(workflow.job_tracker, "get_status", return_value=idle_state()), \
             patch.object(workflow, "_manual_job_set") as stage, \
             patch.object(stop_service, "clear_stop") as clear, \
             patch.object(disk_guard, "ensure_free_space") as disk:
            self.assertFalse(workflow._wait_pipeline_free("fixture", max_wait_s=1))
        stage.assert_called_once()
        clear.assert_not_called()
        disk.assert_not_called()

    def test_idle_poll_does_not_clear_stop_before_ownership(self):
        import sys
        from backend import workflow_api as workflow
        import stop_service
        import disk_guard

        state = SimpleNamespace(stop_requested=True)
        main = SimpleNamespace(UNIFIED_PIPELINE_LOCK=SimpleNamespace(locked=lambda: False))
        with patch.dict(sys.modules, {"main": main, "shared_state": state}), \
             patch.object(workflow.job_tracker, "get_status", return_value=idle_state()), \
             patch.object(stop_service, "is_stop_requested", side_effect=[True, False]), \
             patch.object(stop_service, "clear_stop") as clear, \
             patch.object(disk_guard, "ensure_free_space") as disk:
            self.assertTrue(workflow._wait_pipeline_free("fixture"))
        self.assertTrue(state.stop_requested)
        clear.assert_not_called()
        disk.assert_not_called()

    def test_idle_poll_defers_stop_validation_to_owned_scope(self):
        import sys
        from backend import workflow_api as workflow
        from backend.v1_integrity_errors import PipelineStateError
        import stop_service
        import disk_guard

        main = SimpleNamespace(UNIFIED_PIPELINE_LOCK=SimpleNamespace(locked=lambda: False))
        with patch.dict(sys.modules, {"main": main, "shared_state": SimpleNamespace(stop_requested=True)}), \
             patch.object(workflow.job_tracker, "get_status", return_value=idle_state()), \
             patch.object(stop_service, "is_stop_requested", return_value=True), \
             patch.object(stop_service, "clear_stop"), \
             patch.object(disk_guard, "ensure_free_space") as disk:
            self.assertTrue(workflow._wait_pipeline_free("fixture"))
        disk.assert_not_called()

    def test_manual_cn_honors_timeout_before_extracting_audio(self):
        from backend import workflow_api as workflow

        def thread(*, target, **kwargs):
            return SimpleNamespace(start=target)

        with patch.object(workflow, "_MANUAL_JOBS", {}), \
             patch.object(workflow, "_manual_resolve_raw_video", return_value=Path("fixture.mp4")), \
             patch.object(workflow, "_wait_pipeline_free", return_value=False) as wait, \
             patch.object(workflow.threading, "Thread", side_effect=thread), \
             patch.object(workflow.subprocess, "run") as extract:
            response = asyncio.run(workflow.api_manual_generate_cn({"stem": "fixture"}))
            job_id = response["job_id"]
            self.assertEqual(workflow._MANUAL_JOBS[job_id]["status"], "error")
            wait.assert_called_once_with(job_id)
        extract.assert_not_called()

    def test_manual_translate_honors_timeout_before_translation(self):
        from backend import workflow_api as workflow
        from backend.ai import translation

        def thread(*, target, **kwargs):
            return SimpleNamespace(start=target)

        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "fixture").mkdir()
            (root / "fixture/original.srt").write_text("fixture")
            with patch.object(workflow, "WORKSPACE", root), \
                 patch.object(workflow, "_MANUAL_JOBS", {}), \
                 patch.object(workflow, "_wait_pipeline_free", return_value=False) as wait, \
                 patch.object(workflow.threading, "Thread", side_effect=thread), \
                 patch.object(translation, "translate_subtitles") as translate:
                response = asyncio.run(workflow.api_manual_translate({"stem": "fixture"}))
                job_id = response["job_id"]
                self.assertEqual(workflow._MANUAL_JOBS[job_id]["status"], "error")
                wait.assert_called_once_with(job_id)
            translate.assert_not_called()
            self.assertFalse((root / "fixture/translated.srt").exists())

    def test_invalid_timeline_clip_is_not_silently_omitted(self):
        from backend import workflow_api as workflow

        with tempfile.TemporaryDirectory() as folder:
            source, output = Path(folder) / "source.mp4", Path(folder) / "out.mp4"
            source.write_bytes(b"fixture")
            probe = SimpleNamespace(stdout=json.dumps({"streams": []}), returncode=0)
            with patch.object(workflow.subprocess, "run", return_value=probe), \
                 patch.object(workflow, "_run_ffmpeg_ctl") as render:
                for value in ("broken", float("nan"), float("inf"), None):
                    with self.subTest(value=value), self.assertRaises(ValueError):
                        workflow._apply_timeline_edits_file(source, [{"start": 0, "end": 1}, {"start": value}], output)
            render.assert_not_called()
            self.assertFalse(output.exists())
