import json
import importlib
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from backend import telegram_bot as bot
from backend import telegram_queue_monitor as monitor
from backend.v1_quality_gate import CriticalQualityError


class TelegramV1Tests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.media = importlib.import_module("pipeline_v2.download_validation")
        self.checkpoint = importlib.import_module("v1_checkpoint")
        self.critical_error = importlib.import_module("v1_quality_gate").CriticalQualityError
        self.source = self.root / "downloads" / "input.mp4"
        self.source.parent.mkdir()
        self.source.write_bytes(b"source fixture")
        self.job = {
            "type": "url", "url": "https://example.com/video", "pos": 1,
            "job_id": "telegram_stable", "voice_mode": "manual", "retry_count": 1,
            "job_overrides": {"voice_mode": "manual", "voice_config": {"manual_voice": {"id": "chosen"}},
                              "feature_flags": {"V1_VIDEO_MODE": "SHORT"}, "bgm_volume_db": -17},
            "resume_state": {"source_path": str(self.source), "source_sha256": "source-hash"},
        }
        for target, value in [
            ("WORKSPACE", str(self.root)), ("OUTPUT_DIR", str(self.root / "delivery")),
            ("global_queue", monitor.MonitoredQueue()),
        ]:
            p = patch.object(bot, target, value)
            p.start()
            self.addCleanup(p.stop)
        p = patch.object(monitor, "SNAPSHOT", self.root / "telegram_queue.json")
        p.start()
        self.addCleanup(p.stop)
        state = SimpleNamespace(stop_requested=False)
        p = patch.object(bot, "shared_state", state)
        p.start()
        self.addCleanup(p.stop)
        p = patch.dict("sys.modules", {"shared_state": state})
        p.start()
        self.addCleanup(p.stop)

    async def test_shared_orchestrator_receives_frozen_contract_and_finishes_once(self):
        execute = AsyncMock(return_value={
            "status": "success", "final_video": str(self.root / "delivery" / "Dubbed_input.mp4"),
            "tracker_finalized": True,
        })
        with patch("v1_orchestrator.V1Orchestrator", return_value=SimpleNamespace(execute_job=execute)), \
             patch.object(bot.job_tracker, "finish_video") as finish:
            await bot.run_pipeline_v1_for_telegram(self.source, self.job, None)
        args = execute.call_args.kwargs
        self.assertEqual(args["job_id"], "telegram_stable")
        self.assertEqual(args["overrides"]["bgm_volume_db"], -17)
        self.assertEqual(args["user_mode"], "SHORT")
        self.assertTrue(args["resume_if_possible"])
        self.assertEqual(args["delivery_path"], self.root / "delivery" / "Dubbed_input.mp4")
        self.assertFalse(args["stop_checker"]())
        bot.shared_state.stop_requested = True
        self.assertTrue(args["stop_checker"]())
        finish.assert_not_called()

    async def test_qc_block_does_not_retry_or_claim_completion(self):
        with patch.object(self.media, "probe_downloaded_video"), \
             patch.object(self.checkpoint, "fingerprint", return_value="source-hash"), \
             patch.object(bot, "run_pipeline_v1_for_telegram", side_effect=self.critical_error("critical QC")), \
             patch.object(bot.job_tracker, "start_video"), \
             patch.object(bot.job_tracker, "set_error") as error, \
             patch.object(bot.job_tracker, "finish_video") as finish, \
             patch.object(bot, "safe_edit_status", new_callable=AsyncMock) as status:
            result = await bot._process_telegram_job(self.job)
        self.assertEqual(result["status"], "failed")
        self.assertTrue(bot.global_queue.empty())
        error.assert_called_once()
        finish.assert_not_called()
        self.assertFalse(any("Hoàn tất" in str(call) for call in status.call_args_list))

    async def test_retry_keeps_source_identity_settings_and_does_not_download_again(self):
        with patch.object(self.media, "probe_downloaded_video"), \
             patch.object(self.checkpoint, "fingerprint", return_value="source-hash"), \
             patch("social_downloader.download_social_video") as download, \
             patch.object(bot, "run_pipeline_v1_for_telegram", side_effect=RuntimeError("temporary TTS failure")), \
             patch.object(bot.job_tracker, "start_video"), \
             patch.object(bot.job_tracker, "set_error"), \
             patch.object(bot, "safe_edit_status", new_callable=AsyncMock):
            await bot._process_telegram_job(self.job)
        retry = await bot.global_queue.get()
        self.assertEqual(retry["job_id"], self.job["job_id"])
        self.assertEqual(retry["resume_state"], self.job["resume_state"])
        self.assertEqual(retry["job_overrides"], self.job["job_overrides"])
        self.assertEqual(retry["retry_count"], 2)
        download.assert_not_called()
        bot.global_queue.task_done()

    async def test_source_change_is_rejected_before_orchestration(self):
        with patch.object(self.media, "probe_downloaded_video"), \
             patch.object(self.checkpoint, "fingerprint", return_value="changed"), \
             patch.object(bot, "run_pipeline_v1_for_telegram", new_callable=AsyncMock) as execute, \
             patch.object(bot.job_tracker, "start_video"), \
             patch.object(bot.job_tracker, "set_error"), \
             patch.object(bot, "safe_edit_status", new_callable=AsyncMock):
            result = await bot._process_telegram_job(self.job)
        self.assertEqual(result["status"], "failed")
        execute.assert_not_awaited()
        self.assertTrue(bot.global_queue.empty())

    async def test_deterministic_timing_failure_is_not_requeued(self):
        from v1_speech_guard import SpeechTimingError
        error = RuntimeError("Voice worker failed")
        error.__cause__ = SpeechTimingError("Cue exceeds its reading window")
        with patch.object(self.media, "probe_downloaded_video"), \
             patch.object(self.checkpoint, "fingerprint", return_value="source-hash"), \
             patch.object(bot, "run_pipeline_v1_for_telegram", side_effect=error), \
             patch.object(bot.job_tracker, "start_video"), \
             patch.object(bot.job_tracker, "set_error"), \
             patch.object(bot, "safe_edit_status", new_callable=AsyncMock):
            result = await bot._process_telegram_job(self.job)
        self.assertEqual(result["status"], "failed")
        self.assertTrue(bot.global_queue.empty())

    async def test_active_job_survives_restart_with_all_resume_fields(self):
        await bot.global_queue.put(self.job)
        await bot.global_queue.get()
        snapshot = json.loads(monitor.SNAPSHOT.read_text(encoding="utf-8"))
        self.assertEqual(snapshot["items"], [])
        self.assertEqual(snapshot["active_job"]["job_id"], "telegram_stable")
        restored = monitor.MonitoredQueue()
        with patch.object(bot, "global_queue", restored), \
             patch.object(bot, "worker_task", SimpleNamespace(done=lambda: False)):
            count = await bot.enqueue_pending_queue_jobs()
        self.assertEqual(count, 1)
        job = await restored.get()
        self.assertEqual(job["resume_state"], self.job["resume_state"])
        self.assertEqual(job["job_overrides"], self.job["job_overrides"])
        self.assertEqual(job["retry_count"], 1)
        restored.task_done()

    async def test_pending_clear_preserves_active_job_and_queue_accounting(self):
        await bot.global_queue.put(self.job)
        await bot.global_queue.get()
        await bot.global_queue.put(dict(self.job, job_id="telegram_pending"))
        bot.global_queue.clear()
        snapshot = json.loads(monitor.SNAPSHOT.read_text(encoding="utf-8"))
        self.assertEqual(snapshot["items"], [])
        self.assertEqual(snapshot["active_job"]["job_id"], "telegram_stable")
        bot.global_queue.task_done()
        await bot.global_queue.join()
        self.assertIsNone(json.loads(monitor.SNAPSHOT.read_text(encoding="utf-8"))["active_job"])
