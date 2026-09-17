import tempfile
import unittest
from pathlib import Path

from backend.pipeline_v2.batching import bounded_batches, chunked
from backend.pipeline_v2.artifact_store import hash_file
from backend.pipeline_v2.manifest import ManifestStore
from backend.pipeline_v2.mixer import plan_voice_chunks
from backend.pipeline_v2.models import FingerprintSet
from backend.pipeline_v2.resume import find_resumable_jobs
from backend.pipeline_v2.video_pipeline import V2_STAGE_ORDER


class ResourceBatchingTests(unittest.TestCase):
    def test_bounded_batches_preserve_order_and_obey_limits(self):
        items = ["aa", "bbb", "c", "dddd", "ee"]
        batches = bounded_batches(items, 3, 5, len)
        self.assertEqual([item for batch in batches for item in batch], items)
        self.assertTrue(all(len(batch) <= 3 for batch in batches))
        self.assertTrue(all(sum(map(len, batch)) <= 5 for batch in batches))
        self.assertEqual(chunked(items, 2)[-1], ["ee"])

    def test_voice_chunks_cover_timeline_without_cutting_crossing_dub(self):
        dubs = [
            {"index": 1, "start": 0.2, "end": 0.7},
            {"index": 2, "start": 0.9, "end": 1.3},
            {"index": 3, "start": 1.4, "end": 1.6},
        ]
        chunks = plan_voice_chunks(dubs, 2.0, 1.0)
        self.assertEqual(chunks[0].start_seconds, 0.0)
        self.assertGreaterEqual(chunks[0].end_seconds, 1.3)
        self.assertEqual(chunks[-1].end_seconds, 2.0)
        self.assertEqual(
            [item["index"] for chunk in chunks for item in chunk.dubs],
            [1, 2, 3],
        )


class StartupResumeDiscoveryTests(unittest.TestCase):
    def test_interrupted_manifest_is_discovered_without_secrets(self):
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            source = workspace / "source.mp4"
            source.write_bytes(b"video")
            job_directory = workspace / "job-1"
            manifest_directory = job_directory / "pipeline_v2"
            output = job_directory / "final.mp4"
            store = ManifestStore(manifest_directory)
            store.create(
                "job-1",
                FingerprintSet("source", "config", {}),
                stage_names=V2_STAGE_ORDER,
                metadata={
                    "mode": "v2",
                    "source_path": str(source),
                    "request": {
                        "output_path": str(output),
                        "target_lang": "vi",
                        "voice_source": "edge",
                    },
                },
            )
            jobs = find_resumable_jobs(workspace)
            self.assertEqual(len(jobs), 1)
            self.assertEqual(jobs[0].job_id, "job-1")
            self.assertEqual(jobs[0].next_stage, "input")

    def test_same_size_corrupt_delivery_is_resumable(self):
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            source = workspace / "source.mp4"
            source.write_bytes(b"video")
            output = workspace / "published.mp4"
            output.write_bytes(b"good-output")
            sha256, size = hash_file(output)
            manifest_directory = workspace / "job-1" / "pipeline_v2"
            store = ManifestStore(manifest_directory)
            manifest = store.create(
                "job-1",
                FingerprintSet("source", "config", {}),
                stage_names=V2_STAGE_ORDER,
                metadata={
                    "source_path": str(source),
                    "request": {"output_path": str(output)},
                },
            )
            manifest.start_stage("deliver")
            manifest.stage("deliver").metadata["published_outputs"] = [
                {
                    "path": str(output),
                    "size_bytes": size,
                    "sha256": sha256,
                }
            ]
            manifest.complete_stage("deliver")
            store.save(manifest)
            self.assertEqual(find_resumable_jobs(workspace), [])

            output.write_bytes(b"evil-output")
            self.assertEqual(output.stat().st_size, size)
            jobs = find_resumable_jobs(workspace)
            self.assertEqual(len(jobs), 1)


if __name__ == "__main__":
    unittest.main()
