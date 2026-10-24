import json
import multiprocessing
import tempfile
import unittest
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

from backend import job_config_service as service


def _save_draft_process(folder, index, start, results):
    try:
        from backend import job_config_service as child
        child.DRAFTS_FILE = Path(folder) / "drafts.json"
        start.wait(15)
        child.save_video_draft(f"video-{index}.mp4", {"overrides": {"speed": 1.0}})
        results.put(None)
    except Exception as error:
        results.put(repr(error))


def _read_idempotency_process(folder, results):
    from backend import job_config_service as child
    child.CONTROL_DIR = Path(folder)
    results.put(child.check_idempotency("request"))


class ConfigTransactionTests(unittest.TestCase):
    def test_multiprocess_drafts_preserve_every_update(self):
        with tempfile.TemporaryDirectory() as folder:
            context = multiprocessing.get_context("spawn")
            start, results = context.Event(), context.Queue()
            processes = [context.Process(target=_save_draft_process, args=(folder, i, start, results)) for i in range(4)]
            try:
                for process in processes:
                    process.start()
                start.set()
                self.assertEqual([results.get(timeout=30) for _ in processes], [None] * 4)
                for process in processes:
                    process.join(10)
                    self.assertEqual(process.exitcode, 0)
                saved = json.loads((Path(folder) / "drafts.json").read_text(encoding="utf-8"))
                self.assertEqual(set(saved), {f"video-{i}.mp4" for i in range(4)})
            finally:
                for process in processes:
                    if process.is_alive():
                        process.terminate()
                    process.join()
                results.close()

    def seed(self, folder):
        path = Path(folder) / "control/job_frozen_configs.json"
        snapshot = {"snapshot_id": "A", "job_id": "A", "video_name": "same.mp4", "revision": 1,
                    "preset_id": "old-preset", "overrides": {"bgm_volume_db": -17},
                    "effective_config": {"bgm_volume_db": -17, "qc_policy": "BLOCK", "voice_mode": "manual",
                                         "voice_config": {"manual_voice": {"id": "voice-A"}}}}
        service._atomic_write_json(path, {"A": snapshot, "same.mp4": snapshot})
        return path

    def test_partial_update_keeps_frozen_values_even_if_preset_changed(self):
        with tempfile.TemporaryDirectory() as folder:
            self.seed(folder)
            with patch.object(service, "get_preset", return_value={"config": {"bgm_volume_db": -2}}):
                result = service.update_queued_job_config("A", overrides={"dubbing_volume_db": 3}, workspace_path=folder)
            self.assertEqual(result["effective_config"]["bgm_volume_db"], -17)
            self.assertEqual(result["effective_config"]["qc_policy"], "BLOCK")
            self.assertEqual(result["effective_config"]["voice_config"]["manual_voice"]["id"], "voice-A")

    def test_revision_is_checked_under_the_write_lock(self):
        with tempfile.TemporaryDirectory() as folder:
            self.seed(folder)
            def update():
                try:
                    service.update_queued_job_config("A", overrides={"speed": 1.1}, expected_revision=1, workspace_path=folder)
                    return "saved"
                except ValueError:
                    return "conflict"
            with ThreadPoolExecutor(max_workers=2) as pool:
                self.assertCountEqual(list(pool.map(lambda _: update(), range(2))), ["saved", "conflict"])

    def test_clearing_or_editing_old_job_preserves_new_filename_alias(self):
        with tempfile.TemporaryDirectory() as folder:
            path = self.seed(folder)
            data = service.list_frozen_configs(folder)
            newer = {**data["A"], "snapshot_id": "B", "job_id": "B"}
            data.update({"B": newer, "same.mp4": newer})
            service._atomic_write_json(path, data)
            service.update_queued_job_config("A", overrides={"speed": 1.1}, workspace_path=folder)
            self.assertEqual(service.get_frozen_config("same.mp4", folder)["job_id"], "B")
            service.clear_frozen_config("A", folder)
            self.assertEqual(service.get_frozen_config("same.mp4", folder)["job_id"], "B")

    def test_damaged_store_is_not_replaced(self):
        with tempfile.TemporaryDirectory() as folder, patch.object(service, "DRAFTS_FILE", Path(folder) / "drafts.json"):
            service.DRAFTS_FILE.write_text("{broken", encoding="utf-8")
            with self.assertRaises(ValueError):
                service.save_video_draft("video.mp4", {})
            self.assertEqual(service.DRAFTS_FILE.read_text(), "{broken")

    def test_idempotency_survives_a_new_process_and_expires(self):
        with tempfile.TemporaryDirectory() as folder, patch.object(service, "CONTROL_DIR", Path(folder)):
            service.record_idempotency("request", {"batch_id": "one"})
            context = multiprocessing.get_context("spawn")
            results = context.Queue()
            process = context.Process(target=_read_idempotency_process, args=(folder, results))
            process.start()
            try:
                self.assertEqual(results.get(timeout=20), {"batch_id": "one"})
                process.join(10)
                self.assertEqual(process.exitcode, 0)
            finally:
                if process.is_alive():
                    process.terminate()
                process.join()
                results.close()
            with patch.object(service.time, "time", return_value=__import__("time").time() + 301):
                self.assertIsNone(service.check_idempotency("request"))
