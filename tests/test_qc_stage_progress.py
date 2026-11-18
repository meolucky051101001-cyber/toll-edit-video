"""QC has its own checkpoint and clock; old published jobs stay published."""
import copy
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from backend.v1_checkpoint import ManifestManager, STAGES_ORDER
import job_tracker


class QcManifestTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.source = self.root / "source.mp4"
        self.source.write_bytes(b"verified source video")
        self.rendered = self.root / "render.mp4"
        self.rendered.write_bytes(b"verified rendered video")
        self.manager = ManifestManager(self.root)
        self.manager.get_or_create_manifest("qc-test", self.source)

    def complete_legacy_stages(self):
        required = {"separate_vocals": ["vocals_audio", "no_vocals_audio"],
                    "visual_ocr": ["ocr_data"], "ai_translation": ["translation_data"],
                    "ai_tts": ["tts_data", "ass_path", "voice_lock"]}
        for stage in STAGES_ORDER[:-1]:
            self.manager.record_stage_start("qc-test", stage)
            self.manager.record_stage_complete("qc-test", stage,
                {key: str(self.rendered) for key in required.get(stage, ["artifact"])})

    def test_legacy_unpublished_manifest_resumes_at_qc_with_all_eight_artifacts(self):
        self.complete_legacy_stages()
        manifest = self.manager.load_manifest("qc-test")
        manifest.stages.pop("quality_gate")
        manifest.current_stage = "video_rendering"
        self.manager.save_manifest(manifest)
        migrated = self.manager.load_manifest("qc-test")
        self.assertEqual(migrated.current_stage, "quality_gate")
        self.assertEqual(migrated.stages["quality_gate"]["status"], "pending")
        can_resume, next_stage, plan = self.manager.verify_manifest_resumability("qc-test")
        self.assertTrue(can_resume)
        self.assertEqual(next_stage, "quality_gate")
        self.assertEqual(plan["verified_stages"], STAGES_ORDER[:-1])

    def test_legacy_published_manifest_is_not_reopened(self):
        manifest = self.manager.load_manifest("qc-test")
        manifest.stages.pop("quality_gate")
        manifest.status = "completed"
        manifest.output_video_path = str(self.rendered)
        self.manager.save_manifest(manifest)
        migrated = self.manager.load_manifest("qc-test")
        self.assertEqual(migrated.status, "completed")
        self.assertEqual(migrated.stages["quality_gate"]["status"], "completed")
        self.assertEqual(self.manager.verify_manifest_resumability("qc-test")[:2], (False, "completed"))

    def test_qc_retry_and_publish_artifact_update_do_not_extend_render_duration(self):
        self.manager.record_stage_start("qc-test", "video_rendering")
        manifest = self.manager.load_manifest("qc-test")
        manifest.stages["video_rendering"]["started_at"] -= 30
        self.manager.save_manifest(manifest)
        self.manager.record_stage_complete("qc-test", "video_rendering", {"temp_render": str(self.rendered)})
        before = copy.deepcopy(self.manager.load_manifest("qc-test").stages["video_rendering"])
        self.manager.record_stage_start("qc-test", "quality_gate")
        self.manager.record_stage_failure("qc-test", "quality_gate", "decode interrupted")
        self.manager.record_stage_start("qc-test", "quality_gate")
        retry = self.manager.load_manifest("qc-test")
        self.assertEqual(retry.stages["quality_gate"]["retry_count"], 2)
        self.assertIsNone(retry.stages["quality_gate"]["error"])
        self.assertIsNone(retry.error_message)
        self.manager.record_stage_complete("qc-test", "quality_gate")
        self.manager.record_stage_complete("qc-test", "video_rendering", {"temp_render": str(self.rendered)})
        after = self.manager.load_manifest("qc-test")
        self.assertEqual(after.stages["video_rendering"]["duration_s"], before["duration_s"])
        self.assertEqual(after.stages["video_rendering"]["completed_at"], before["completed_at"])
        self.assertEqual(after.current_stage, "quality_gate")
        self.assertEqual(after.status, "awaiting_publish")


class QcTrackerTests(unittest.TestCase):
    def setUp(self):
        self.now = 100.0
        patches = [patch.object(job_tracker, "_CURRENT_STATE", copy.deepcopy(job_tracker._DEFAULT_STATE)),
                   patch.object(job_tracker, "_sync_from_disk_unlocked"),
                   patch.object(job_tracker, "_save_state_to_disk"),
                   patch.object(job_tracker, "_read_batch_lock_unlocked", return_value=None),
                   patch.object(job_tracker.time, "time", side_effect=lambda: self.now)]
        for mocked in patches:
            mocked.start()
            self.addCleanup(mocked.stop)

    def test_qc_progress_locks_render_clock_and_keeps_phase_start_and_eta(self):
        job_tracker.start_video("one.mp4", worker_id=1)
        job_tracker.update_step(8, "Render", stage="video_rendering", total_steps=9, worker_id=1)
        self.now = 130
        job_tracker.update_step(9, "Decode", stage="quality_gate", total_steps=9, worker_id=1,
                                details={"phase": "decode", "completed": 5, "total": 10})
        self.now = 150
        state = job_tracker.get_status()
        self.assertEqual(state["step_durations"]["8"], 30)
        self.assertEqual(state["step_durations"]["9"], 20)
        self.assertIsNone(state["eta_seconds"])
        self.assertEqual(state["status"], "running")
        job_tracker.update_step(9, "OCR", stage="quality_gate", total_steps=9, worker_id=1,
                                details={"phase": "ocr", "completed": 0, "total": 10})
        self.now = 160
        job_tracker.update_step(9, "OCR", percent=98, stage="quality_gate", total_steps=9, worker_id=1,
                                details={"phase": "ocr", "completed": 5, "total": 10})
        state = job_tracker.get_status()
        self.assertEqual(state["eta_seconds"], 10)
        self.assertEqual(state["phase_started_at"], 150)
        self.assertEqual(state["current_step_start"], 130)
        self.assertEqual(state["step_durations"]["8"], 30)

    def test_workers_have_independent_qc_durations_and_next_job_resets_qc(self):
        job_tracker.start_video("one.mp4", worker_id=1)
        job_tracker.update_step(8, "Render", total_steps=9, worker_id=1)
        self.now = 110
        job_tracker.start_video("two.mp4", worker_id=2)
        job_tracker.update_step(8, "Render", total_steps=9, worker_id=2)
        self.now = 130
        job_tracker.update_step(9, "QC", total_steps=9, worker_id=1)
        self.now = 150
        job_tracker.update_step(9, "QC", total_steps=9, worker_id=2)
        state = job_tracker.get_status()
        self.assertEqual(state["workers"]["1"]["step_durations"]["8"], 30)
        self.assertEqual(state["workers"]["1"]["step_durations"]["9"], 20)
        self.assertEqual(state["workers"]["2"]["step_durations"]["8"], 40)
        self.assertEqual(state["workers"]["2"]["step_durations"]["9"], 0)
        self.now = 160
        job_tracker.start_video("three.mp4", worker_id=1)
        state = job_tracker.get_status()
        self.assertEqual(state["workers"]["1"]["step_durations"], {})
        self.assertEqual(state["workers"]["1"]["details"], {})
        self.assertIsNone(state["workers"]["1"]["stage"])
        self.assertEqual(state["workers"]["2"]["step"], 9)

    def test_fixed_stage_percent_is_not_an_eta_but_legacy_eight_steps_still_work(self):
        job_tracker.start_video("one.mp4", worker_id=1)
        job_tracker.update_step(8, "Render", percent=95, total_steps=9, worker_id=1)
        self.now = 200
        self.assertIsNone(job_tracker.get_status()["eta_seconds"])
        job_tracker.update_step(8, "Legacy render", percent=95, total_steps=8, worker_id=1)
        self.assertEqual(job_tracker.get_status()["eta_seconds"], 5)

    def test_interleaved_decode_uses_measured_stage_progress_after_ocr_batch(self):
        job_tracker.start_video("one.mp4", worker_id=1)
        job_tracker.update_step(9, "Decode", stage="quality_gate", worker_id=1,
            details={"phase": "decode", "recognized": 0, "stage_fraction": .25})
        self.now = 120
        self.assertIsNone(job_tracker.get_status()["eta_seconds"])
        job_tracker.update_step(9, "Decode", stage="quality_gate", worker_id=1,
            details={"phase": "decode", "recognized": 5, "stage_fraction": .75})
        self.assertEqual(job_tracker.get_status()["eta_seconds"], 6)

    def test_verified_publication_freezes_qc_clock_and_keeps_ninth_step(self):
        import dashboard_media
        import delivery_ledger
        import render_history
        job_tracker.start_video("one.mp4", worker_id=1)
        job_tracker.update_step(8, "Render", total_steps=9, worker_id=1)
        self.now = 130
        job_tracker.update_step(9, "QC", total_steps=9, worker_id=1)
        self.now = 150
        with patch.object(dashboard_media, "media_status", return_value={"status": "valid"}), \
             patch.object(delivery_ledger, "record"), patch.object(render_history, "record_render_duration"):
            job_tracker.finish_video("one.mp4", "fixture.mp4", duration_seconds=50, worker_id=1)
            self.now = 200
            state = job_tracker.get_status()
        self.assertEqual(state["step"], 9)
        self.assertEqual(state["stage"], "publish")
        self.assertEqual(state["percent"], 100)
        self.assertEqual(state["step_durations"]["8"], 30)
        self.assertEqual(state["step_durations"]["9"], 20)
        self.assertEqual(state["workers"]["1"]["step_durations"]["9"], 20)

