"""Frozen job config store keeps recent snapshots and prunes stale aliases."""
import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from backend import job_config_service

DAY = 86400.0


def snapshot(snapshot_id, created_at, **extra):
    data = {"snapshot_id": snapshot_id, "video_name": f"{snapshot_id}.mp4", "job_id": snapshot_id,
            "effective_config": {"asr_model": "whisper_sensevoice"}, "revision": 1}
    if created_at is not None:
        data["created_at"] = created_at
    data.update(extra)
    return data


class FrozenConfigRetentionTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.workspace = Path(self.temporary.name)
        self.store = self.workspace / "control" / "job_frozen_configs.json"
        self.store.parent.mkdir(parents=True)
        now = time.time()
        entries = {
            "old": snapshot("old", now - 30 * DAY),
            "recent": snapshot("recent", now - 2 * DAY),
            "edited": snapshot("edited", now - 40 * DAY, updated_at=now - DAY),
            "legacy": snapshot("legacy", None),
            "broken": snapshot("broken", "not-a-time"),
        }
        store = {}
        for key, value in entries.items():
            store[key] = value
            store[value["video_name"]] = value
        self.store.write_text(json.dumps(store), encoding="utf-8")
        self.addCleanup(self.temporary.cleanup)

    def freeze(self):
        with patch("backend.v1_video_router.is_router_enabled", return_value=False):
            return job_config_service.freeze_job_config(
                "new.mp4", job_id="job-new", workspace_path=self.workspace)

    def stored(self):
        return json.loads(self.store.read_text(encoding="utf-8"))

    def test_freeze_prunes_every_alias_of_stale_snapshots(self):
        with patch.dict(os.environ, {"V1_FROZEN_CONFIG_RETENTION_DAYS": "14"}):
            self.freeze()
        store = self.stored()
        self.assertNotIn("old", store)
        self.assertNotIn("old.mp4", store)
        for key in ("recent", "recent.mp4", "edited", "edited.mp4", "legacy", "legacy.mp4",
                    "broken", "broken.mp4", "job-new", "new.mp4"):
            self.assertIn(key, store)
        self.assertEqual(store["job-new"]["video_name"], "new.mp4")

    def test_retention_can_be_disabled(self):
        with patch.dict(os.environ, {"V1_FROZEN_CONFIG_RETENTION_DAYS": "0"}):
            self.freeze()
        store = self.stored()
        self.assertIn("old", store)
        self.assertIn("old.mp4", store)

    def test_invalid_retention_falls_back_to_default_window(self):
        with patch.dict(os.environ, {"V1_FROZEN_CONFIG_RETENTION_DAYS": "two weeks"}):
            self.freeze()
        store = self.stored()
        self.assertNotIn("old", store)
        self.assertIn("recent", store)

    def test_pruned_store_still_resolves_kept_jobs(self):
        with patch.dict(os.environ, {"V1_FROZEN_CONFIG_RETENTION_DAYS": "14"}):
            self.freeze()
        found = job_config_service.get_frozen_config("recent", workspace_path=self.workspace)
        self.assertEqual(found["snapshot_id"], "recent")
        self.assertIsNone(job_config_service.get_frozen_config("old", workspace_path=self.workspace))


if __name__ == "__main__":
    unittest.main()
