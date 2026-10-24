import os
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from backend import job_tracker as tracker
from backend import workspace_cleaner as cleaner
from backend.v1_checkpoint import ManifestManager, fingerprint


class StepTimingTests(unittest.TestCase):
    def test_same_step_preserves_worker_and_global_clock(self):
        state = {"step": 0, "workers": {"1": {"step": 0}}}
        with patch.object(tracker, "_CURRENT_STATE", state), \
             patch.object(tracker, "_sync_from_disk_unlocked"), \
             patch.object(tracker, "_save_state_to_disk"), \
             patch.object(tracker, "_get_worker_id", return_value=1):
            with patch.object(tracker.time, "time", return_value=100):
                tracker.update_step(1, "extract", percent=10)
            with patch.object(tracker.time, "time", return_value=110):
                tracker.update_step(1, "extract progress", percent=15)
            self.assertEqual(state["current_step_start"], 100)
            self.assertEqual(state["workers"]["1"]["current_step_start"], 100)
            self.assertEqual(state["updated_at"], 110)
            with patch.object(tracker.time, "time", return_value=120):
                tracker.update_step(2, "separate", percent=20)
            self.assertEqual(state["step_durations"]["1"], 20)
            self.assertEqual(state["workers"]["1"]["step_durations"]["1"], 20)


class RetentionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.ws = self.root / "workspace"
        self.job = self.ws / "video_name"
        self.job.mkdir(parents=True)
        self.source = self.ws / "downloads" / "source.mp4"
        self.source.parent.mkdir()
        subprocess.run(
            ["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "color=red:size=160x90:rate=10",
             "-f", "lavfi", "-i", "sine=frequency=500:sample_rate=16000", "-t", "1",
             "-c:v", "libx264", "-c:a", "aac", str(self.source)],
            check=True, capture_output=True, timeout=30,
        )
        self.final = self.job / "final_source.mp4"
        self.final.write_bytes(self.source.read_bytes())
        self.delivery = self.root / "delivery.mp4"
        self.delivery.write_bytes(self.final.read_bytes())
        self.audio = self.job / "mixed.wav"
        self.audio.write_bytes(b"keep for editing")
        self.scratch = self.job / "segment_fit_in.wav"
        self.scratch.write_bytes(b"disposable scratch")
        self.manager = ManifestManager(self.ws)
        self.manifest = self.manager.get_or_create_manifest(
            "batch_different_id", self.source, effective_config={"output_dir": str(self.job)}
        )
        self.manager.record_stage_complete(self.manifest.job_id, "audio_mixing", {"mixed": str(self.audio)})
        self.manager.record_stage_complete(self.manifest.job_id, "video_rendering", {"temp_render": str(self.final)})
        self.manifest = self.manager.load_manifest(self.manifest.job_id)
        self.manifest.status = "completed"
        self.manifest.output_video_path = str(self.delivery)
        self.manager.save_manifest(self.manifest)

    def test_resolves_actual_directory_and_preserves_checkpoint_media(self):
        expected = self.final.stat().st_size + self.scratch.stat().st_size
        freed = cleaner.clean_completed_job_intermediate_files(self.manifest.job_id, self.ws)
        self.assertEqual(freed, expected)
        self.assertFalse(self.final.exists())
        self.assertFalse(self.scratch.exists())
        self.assertTrue(self.audio.exists())
        self.assertTrue(self.source.exists())

    def test_corrupt_delivery_or_directory_mismatch_does_not_delete(self):
        self.assertEqual(cleaner.clean_completed_job_intermediate_files(
            self.manifest.job_id, self.ws, self.ws / "wrong"), 0)
        self.delivery.write_bytes(b"corrupt")
        self.assertEqual(cleaner.clean_completed_job_intermediate_files(self.manifest.job_id, self.ws), 0)
        self.assertTrue(self.final.exists())
        self.assertTrue(self.scratch.exists())
        self.assertTrue(self.audio.exists())

    def test_workspace_is_only_copy_and_must_remain(self):
        self.manifest.output_video_path = str(self.final)
        self.manager.save_manifest(self.manifest)
        scratch_size = self.scratch.stat().st_size
        self.assertEqual(cleaner.clean_completed_job_intermediate_files(self.manifest.job_id, self.ws),
                         scratch_size)
        self.assertTrue(self.final.exists())

    def test_unfinished_job_and_shared_directory_are_protected(self):
        new = self.manager.get_or_create_manifest(
            "next_job", self.source, effective_config={"output_dir": str(self.job)}
        )
        new.status = "in_progress"
        self.manager.save_manifest(new)
        self.assertEqual(cleaner.clean_completed_job_intermediate_files(self.manifest.job_id, self.ws), 0)
        old = time.time() - 10 * 86400
        os.utime(self.source, (old, old))
        with patch.object(cleaner, "get_disk_free_gb", return_value=0):
            self.assertEqual(cleaner.clean_stale_downloads(self.source.parent), 0)
        self.assertTrue(self.source.exists())
        self.assertTrue(self.audio.exists())

    def test_retention_uses_completed_manifest_age(self):
        self.assertEqual(cleaner.clean_stale_workspace_jobs(self.ws), 0)
        self.assertTrue(self.scratch.exists())
        self.manifest.updated_at = time.time() - 48 * 3600
        with patch("v1_checkpoint.time.time", return_value=self.manifest.updated_at):
            self.manager.save_manifest(self.manifest)
        self.assertGreater(cleaner.clean_stale_workspace_jobs(self.ws), 0)
        self.assertTrue(self.audio.exists())

    def test_manifest_flushes_before_publish_and_keeps_old_state_on_failure(self):
        import importlib
        atomic = importlib.import_module("pipeline_v2.atomic_io")
        with patch.object(atomic.os, "fsync", wraps=os.fsync) as sync:
            self.manager.save_manifest(self.manifest)
        self.assertGreaterEqual(sync.call_count, 1)
        previous = self.manager._manifest_file(self.manifest.job_id).read_bytes()
        self.manifest.status = "failed"
        with patch.object(atomic.os, "fsync", side_effect=OSError("disk write failure")):
            with self.assertRaisesRegex(RuntimeError, "Cannot persist checkpoint"):
                self.manager.save_manifest(self.manifest)
        self.assertEqual(self.manager._manifest_file(self.manifest.job_id).read_bytes(), previous)
