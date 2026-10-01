import os
import unittest
from unittest import mock
import tempfile
from pathlib import Path

from backend.v1_checkpoint import (
    JobManifest,
    ManifestManager,
    Checkpoint,
    fingerprint,
    STAGES_ORDER,
)
from backend.v1_orchestrator import V1Orchestrator


class CheckpointOrchestratorTests(unittest.TestCase):

    def test_legacy_checkpoint_compatibility(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp = Path(tmp_dir)
            src = tmp / "source.txt"
            src.write_text("test source content", encoding="utf-8")
            out = tmp / "output.txt"
            out.write_text("test output content", encoding="utf-8")

            # Check fingerprint
            fp = fingerprint(src)
            self.assertTrue(len(fp) == 64)

            # Check Checkpoint class
            cp = Checkpoint(src, [out], "setting_v1")
            self.assertFalse(cp.hit())
            cp.save()
            self.assertTrue(cp.hit())

    def test_job_manifest_creation_and_atomic_save(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            mgr = ManifestManager(tmp_dir)
            video_file = Path(tmp_dir) / "sample.mp4"
            video_file.write_bytes(b"dummy video bytes" * 10)

            manifest = mgr.get_or_create_manifest(
                job_id="job_test_001",
                video_path=video_file,
                video_mode="SHORT",
                effective_config={"voice": "hoaimy"},
            )

            self.assertEqual(manifest.job_id, "job_test_001")
            self.assertEqual(manifest.video_mode, "SHORT")
            self.assertEqual(manifest.status, "created")
            self.assertEqual(len(manifest.stages), len(STAGES_ORDER))
            self.assertIn("extract_audio", manifest.stages)

            # Reload from disk
            reloaded = mgr.load_manifest("job_test_001")
            self.assertIsNotNone(reloaded)
            self.assertEqual(reloaded.job_id, "job_test_001")
            self.assertEqual(reloaded.effective_config.get("voice"), "hoaimy")

    def test_stage_transitions_and_artifacts(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            mgr = ManifestManager(tmp_dir)
            video_file = Path(tmp_dir) / "sample.mp4"
            video_file.write_bytes(b"dummy video data")

            manifest = mgr.get_or_create_manifest("job_test_002", video_file, "MEDIUM")

            # Start stage 1
            mgr.record_stage_start("job_test_002", "extract_audio")
            loaded = mgr.load_manifest("job_test_002")
            self.assertEqual(loaded.status, "in_progress")
            self.assertEqual(loaded.stages["extract_audio"]["status"], "running")
            self.assertEqual(loaded.stages["extract_audio"]["retry_count"], 1)

            # Create an artifact and complete stage 1
            wav_file = Path(tmp_dir) / "original.wav"
            wav_file.write_bytes(b"audio wave header and samples")

            mgr.record_stage_complete(
                "job_test_002",
                "extract_audio",
                artifacts={"original_audio": str(wav_file)}
            )

            completed_m = mgr.load_manifest("job_test_002")
            self.assertEqual(completed_m.stages["extract_audio"]["status"], "completed")
            self.assertIn("original_audio", completed_m.stages["extract_audio"]["artifacts"])
            self.assertEqual(completed_m.current_stage, "separate_vocals")

    def test_batch_progress_tracking_tts(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            mgr = ManifestManager(tmp_dir)
            video_file = Path(tmp_dir) / "sample.mp4"
            video_file.write_bytes(b"dummy video")

            mgr.get_or_create_manifest("job_test_tts", video_file)

            # Record segments 1, 2, 3
            mgr.record_tts_segment_done("job_test_tts", 1)
            mgr.record_tts_segment_done("job_test_tts", 2)
            mgr.record_tts_segment_done("job_test_tts", 3)
            # Duplicate record should not add twice
            mgr.record_tts_segment_done("job_test_tts", 2)

            loaded = mgr.load_manifest("job_test_tts")
            self.assertEqual(loaded.tts_completed_segments, [1, 2, 3])

    def test_resumability_verification(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            mgr = ManifestManager(tmp_dir)
            video_file = Path(tmp_dir) / "sample.mp4"
            video_file.write_bytes(b"real source video content")

            mgr.get_or_create_manifest("job_resume_01", video_file)

            # Complete stage 1
            wav_file = Path(tmp_dir) / "original.wav"
            wav_file.write_bytes(b"wave content 12345")
            mgr.record_stage_start("job_resume_01", "extract_audio")
            mgr.record_stage_complete("job_resume_01", "extract_audio", {"wav": str(wav_file)})

            # Check resumability
            can_resume, next_stage, plan = mgr.verify_manifest_resumability("job_resume_01")
            self.assertTrue(can_resume)
            self.assertEqual(next_stage, "separate_vocals")
            self.assertIn("extract_audio", plan["verified_stages"])

            # Corrupt artifact (modify wave file content so hash mismatches)
            wav_file.write_bytes(b"tampered content")
            can_resume_bad, next_bad, _ = mgr.verify_manifest_resumability("job_resume_01")
            self.assertFalse(can_resume_bad)
            self.assertEqual(next_bad, "extract_audio")

    def test_orchestrator_planning(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            orch = V1Orchestrator(workspace_path=tmp_dir)
            video_file = Path(tmp_dir) / "short_clip.mp4"
            video_file.write_bytes(b"dummy clip data")

            with mock.patch("backend.v1_video_router.extract_media_metadata") as mock_extract:
                from backend.v1_video_router import VideoMetadata
                mock_extract.return_value = VideoMetadata(duration_s=45.0, width=1920, height=1080, fps=30.0)

                plan = orch.plan_job(video_file, "job_orch_01", user_mode="auto")
                self.assertEqual(plan["job_id"], "job_orch_01")
                self.assertEqual(plan["routing"]["resolved_mode"], "SHORT")
                self.assertIn("separation", plan["planned_models"])
                self.assertIn("asr", plan["planned_models"])
                self.assertIn("tts", plan["planned_models"])


if __name__ == "__main__":
    unittest.main()
