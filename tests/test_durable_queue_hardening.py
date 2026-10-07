import json
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path
from backend.durable_jobs import JobStore
from backend.durable_adapter import compute_source_fingerprint


class DurableQueueHardeningTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'hardening_queue.sqlite3'
        self.store = JobStore(self.path)

    def test_retry_reuses_exact_same_job_id_without_creating_new_job(self):
        # 1. Enqueue job
        job_id = self.store.enqueue("key-1", {"url": "https://example.com/video1"}, source_fingerprint="url:video1")
        self.assertIsNotNone(job_id)

        # 2. Worker claims and fails
        claimed_id, data = self.store.claim()
        self.assertEqual(claimed_id, job_id)
        self.store.finish(job_id, "failed", error="TranscribeTimeout")

        job_info_before = self.store.get_job(job_id)
        self.assertEqual(job_info_before["state"], "failed")
        self.assertEqual(job_info_before["retry_count"], 0)

        # 3. Retry called
        retried_id = self.store.retry(job_id, payload_override={"url": "https://example.com/video1", "attempt": 2})
        # Assert same job_id!
        self.assertEqual(retried_id, job_id)

        # Assert no extra row created
        with self.store.connect() as db:
            total_jobs = db.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
            self.assertEqual(total_jobs, 1)

        job_info_after = self.store.get_job(job_id)
        self.assertEqual(job_info_after["id"], job_id)
        self.assertEqual(job_info_after["state"], "queued")
        self.assertEqual(job_info_after["retry_count"], 1)
        self.assertIsNone(job_info_after["error"])
        self.assertEqual(job_info_after["payload"]["attempt"], 2)

    def test_checkpoint_stage_preserved_across_interrupted_restart(self):
        job_id = self.store.enqueue("key-2", {"task": "render_video"}, source_fingerprint="task:2")
        claimed_id, _ = self.store.claim()
        self.assertEqual(claimed_id, job_id)

        # Record progress at 'demucs' stage
        self.store.checkpoint(job_id, {"task": "render_video", "stage_completed": "demucs"}, stage="transcribe")

        # Bot crashes / restarts
        restarted_store = JobStore(self.path)
        restarted_store.recover()

        # Job claimed after restart
        resumed_id, resumed_payload = restarted_store.claim()
        self.assertEqual(resumed_id, job_id)
        self.assertEqual(resumed_payload.get("_checkpoint_stage"), "transcribe")
        self.assertEqual(resumed_payload.get("stage_completed"), "demucs")

    def test_source_fingerprint_generation(self):
        # File path
        test_file = Path(self.temp.name) / "sample.mp4"
        test_file.write_bytes(b"dummy video data")
        fp_file = compute_source_fingerprint({"path": str(test_file)})
        self.assertTrue(fp_file.startswith("file:sample.mp4:"))

        # URL
        fp_url = compute_source_fingerprint({"url": "https://v.douyin.com/abc1234/?utm_source=test"})
        self.assertEqual(fp_url, "url:https://v.douyin.com/abc1234/")

        # Telegram file_id
        fp_tg = compute_source_fingerprint({"file_id": "BAACAgIAAxkBA..."})
        self.assertEqual(fp_tg, "tg_file:BAACAgIAAxkBA...")

    def test_cancel_distinguishes_individual_vs_all_cancellations(self):
        job1 = self.store.enqueue("job-1", {})
        job2 = self.store.enqueue("job-2", {})
        job3 = self.store.enqueue("job-3", {})

        # Cancel only job2
        self.store.cancel(job_id=job2)
        self.assertEqual(self.store.get_job(job2)["state"], "cancelled")
        self.assertEqual(self.store.get_job(job1)["state"], "queued")
        self.assertEqual(self.store.get_job(job3)["state"], "queued")

        # Cancel all remaining
        self.store.cancel()
        self.assertEqual(self.store.get_job(job1)["state"], "cancelled")
        self.assertEqual(self.store.get_job(job3)["state"], "cancelled")

    def test_schema_migration_preserves_existing_legacy_database(self):
        legacy_path = Path(self.temp.name) / 'legacy.sqlite3'
        conn = sqlite3.connect(legacy_path)
        conn.execute('CREATE TABLE jobs (id INTEGER PRIMARY KEY, dedupe TEXT UNIQUE NOT NULL, payload TEXT NOT NULL, state TEXT NOT NULL, updated REAL NOT NULL, error TEXT)')
        conn.execute("INSERT INTO jobs(dedupe, payload, state, updated) VALUES('old_job', '{}', 'queued', 100.0)")
        conn.commit()
        conn.close()

        migrated_store = JobStore(legacy_path)
        row = migrated_store.get_job(1)
        self.assertEqual(row["dedupe"], "old_job")
        self.assertIsNone(row["source_fingerprint"])
        self.assertEqual(row["retry_count"], 0)


if __name__ == "__main__":
    unittest.main()
