import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from backend.pipeline_v2 import (
    ArtifactStore,
    FingerprintSet,
    InvalidStageTransition,
    JobManifest,
    ManifestStore,
    StageStatus,
    fingerprint_json,
)
from backend.pipeline_v2.atomic_io import atomic_write_text


class AtomicIoTests(unittest.TestCase):
    def test_failed_replace_preserves_previous_file_and_cleans_temp(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "state.json"
            target.write_text("old", encoding="utf-8")

            with mock.patch(
                "backend.pipeline_v2.atomic_io.os.replace",
                side_effect=OSError("simulated replace failure"),
            ):
                with self.assertRaises(OSError):
                    atomic_write_text(target, "new")

            self.assertEqual(target.read_text(encoding="utf-8"), "old")
            self.assertEqual(list(Path(directory).glob("*.tmp")), [])


class ArtifactStoreTests(unittest.TestCase):
    def test_staged_file_is_published_and_detects_corruption(self):
        with tempfile.TemporaryDirectory() as directory:
            store = ArtifactStore(directory)
            staged = store.staging_path("audio/original.wav")
            staged.write_bytes(b"valid-wave-placeholder")

            record = store.commit_staged(staged, "audio/original.wav")
            self.assertFalse(staged.exists())
            self.assertTrue(store.validate(record).valid)

            store.path_for(record.key).write_bytes(b"corrupt")
            validation = store.validate(record)
            self.assertFalse(validation.valid)
            self.assertIn(validation.reason, {"size_mismatch", "sha256_mismatch"})

    def test_rejects_path_traversal(self):
        with tempfile.TemporaryDirectory() as directory:
            store = ArtifactStore(directory)
            with self.assertRaises(ValueError):
                store.put_text("../outside.txt", "unsafe")


class ManifestTests(unittest.TestCase):
    def setUp(self):
        self.fingerprints = FingerprintSet(
            source_sha256="source-v1",
            config_sha256=fingerprint_json({"language": "vi"}),
            model_sha256={"whisper": "large-v3"},
        )

    def test_manifest_round_trip_and_stage_artifact_cache(self):
        with tempfile.TemporaryDirectory() as directory:
            job_dir = Path(directory) / "job-1"
            manifest_store = ManifestStore(job_dir)
            artifact_store = ArtifactStore(job_dir / "artifacts")
            manifest = manifest_store.create(
                "job-1", self.fingerprints, stage_names=("extract", "render")
            )

            manifest.start_stage("extract", input_fingerprint="input-v1")
            artifact = artifact_store.put_text("audio/original.wav", "audio")
            manifest.complete_stage("extract", [artifact])
            manifest_store.save(manifest)

            loaded = manifest_store.load()
            self.assertEqual(loaded.revision, 2)
            self.assertEqual(loaded.stage("extract").status, StageStatus.COMPLETED)
            self.assertTrue(
                manifest_store.stage_cache_is_valid(loaded, "extract", artifact_store)
            )

            parsed = json.loads(manifest_store.path.read_text(encoding="utf-8"))
            self.assertEqual(parsed["job_id"], "job-1")

    def test_interrupted_stage_becomes_retryable_and_counts_attempts(self):
        with tempfile.TemporaryDirectory() as directory:
            store = ManifestStore(Path(directory) / "job-2")
            manifest = store.create(
                "job-2", self.fingerprints, stage_names=("transcribe", "translate")
            )
            manifest.start_stage("transcribe")
            store.save(manifest)

            self.assertEqual(store.recover_interrupted(), ["transcribe"])
            recovered = store.load()
            self.assertEqual(recovered.next_resumable_stage(), "transcribe")
            self.assertEqual(recovered.stage("transcribe").status, StageStatus.FAILED)

            recovered.start_stage("transcribe")
            self.assertEqual(recovered.stage("transcribe").attempts, 2)

    def test_invalid_transition_is_rejected(self):
        manifest = JobManifest.new(
            "job-3", self.fingerprints, stage_names=("render",)
        )
        with self.assertRaises(InvalidStageTransition):
            manifest.complete_stage("render")

    def test_cache_identity_changes_with_source_config_or_model(self):
        manifest = JobManifest.new(
            "job-4", self.fingerprints, stage_names=("render",)
        )
        self.assertTrue(manifest.is_cache_compatible(self.fingerprints))
        changed = FingerprintSet(
            source_sha256="source-v2",
            config_sha256=self.fingerprints.config_sha256,
            model_sha256=self.fingerprints.model_sha256,
        )
        self.assertFalse(manifest.is_cache_compatible(changed))

    def test_invalidate_resets_selected_stage_and_downstream_only(self):
        manifest = JobManifest.new(
            "job-5", self.fingerprints, stage_names=("extract", "asr", "translate")
        )
        for stage_name in ("extract", "asr", "translate"):
            manifest.start_stage(stage_name)
            manifest.complete_stage(stage_name)

        invalidated = manifest.invalidate_from("asr")
        self.assertEqual(invalidated, ["asr", "translate"])
        self.assertEqual(manifest.stage("extract").status, StageStatus.COMPLETED)
        self.assertEqual(manifest.stage("asr").status, StageStatus.PENDING)
        self.assertEqual(manifest.next_resumable_stage(), "asr")


if __name__ == "__main__":
    unittest.main()


