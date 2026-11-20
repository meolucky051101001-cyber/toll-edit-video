import asyncio
import copy
import datetime
import json
import tempfile
import time
import unittest
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import requests
import srt

from backend import telegram_bot as bot
from backend import telegram_queue_monitor as monitor
from backend.pipeline_v2.download_validation import DownloadValidationError
from backend.v1_retry_policy import is_transient_network_error


class TelegramNetworkRetryTests(unittest.IsolatedAsyncioTestCase):
    def _strict_translation_error(self, provider_error=None, bad_quality=False):
        from backend.ai import translation

        def bad_response(*args, **kwargs):
            kwargs["failure_causes"].append(provider_error)
            return ["你好"]

        provider = patch.object(translation, "translate_with_gemini", side_effect=bad_response) if bad_quality else patch(
            "backend.ai.v1_gemini_dispatcher.call_gemini_api", side_effect=provider_error
        )
        with patch.dict("os.environ", {"GEMINI_API_KEY": "fixture-key", "LLM_PROVIDER": "gemini"}, clear=True), \
             patch.object(translation, "load_custom_glossary", return_value={}), \
             patch.object(translation, "extract_video_frames_base64", return_value=[]), \
             patch.object(translation, "mark_gemini_unhealthy"), \
             patch("backend.ai.v1_translation_cache.read_cache", return_value=None), \
             patch("backend.ai.v1_translation_cache.write_cache"), provider:
            with self.assertRaises(RuntimeError) as caught:
                translation.translate_subtitles(
                    [srt.Subtitle(1, datetime.timedelta(), datetime.timedelta(seconds=2), "你好")],
                    strict=True, enable_g4f=False, stop_checker=lambda: False,
                )
        return caught.exception

    async def test_strict_gemini_503_preserves_cause_and_telegram_backoff(self):
        from backend.ai.v1_gemini_dispatcher import GeminiAllModelsFailedError

        cause = GeminiAllModelsFailedError("All candidate models failed status=503")
        error = self._strict_translation_error(cause)
        self.assertIs(error.__cause__, cause)
        self.assertTrue(is_transient_network_error(error))
        for attempt, delay in enumerate((60, 300), 1):
            result, set_error, _ = await self._run_failing_pipeline(error)
            self.assertEqual(result["status"], "retry_scheduled")
            set_error.assert_not_called()
            record = self._retry_state()["jobs"][self.job["job_id"]]
            self.assertEqual((record["attempt"], record["delay_seconds"]), (attempt, delay))
            self._make_due()
            self.assertEqual(await bot.process_due_network_retries(), 1)
            self.job = await bot.global_queue.get()
            bot.global_queue.task_done()
        result, set_error, _ = await self._run_failing_pipeline(error)
        self.assertEqual(result["status"], "failed")
        set_error.assert_called_once()

    def test_strict_gemini_quota_and_deadline_are_transient(self):
        from backend.ai.v1_gemini_dispatcher import GeminiQuotaExhaustedError, GeminiDeadlineError

        for cause in (GeminiQuotaExhaustedError("HTTP 429"), GeminiDeadlineError("Overall deadline")):
            with self.subTest(cause=type(cause).__name__):
                error = self._strict_translation_error(cause)
                self.assertIs(error.__cause__, cause)
                self.assertTrue(is_transient_network_error(error))

    async def test_invalid_translation_requeues_without_network_backoff(self):
        from backend.ai.v1_gemini_dispatcher import GeminiAllModelsFailedError

        error = self._strict_translation_error(GeminiAllModelsFailedError("status=503"), bad_quality=True)
        self.assertIn("conforming to quality rules", str(error))
        self.assertIsNone(error.__cause__)
        self.assertFalse(is_transient_network_error(error))
        result, set_error, _ = await self._run_failing_pipeline(error)
        self.assertEqual(result["status"], "retry_queued")
        self.assertEqual(bot.global_queue.qsize(), 1)
        set_error.assert_not_called()
        self.assertFalse(bot.network_retry_state_path().exists())

    def test_strict_gemini_auth_request_and_config_fail_fast(self):
        from backend.ai.v1_gemini_dispatcher import GeminiAuthError, GeminiRequestError, GeminiConfigError

        for cls in (GeminiAuthError, GeminiRequestError, GeminiConfigError):
            with self.subTest(cause=cls.__name__):
                cause = cls("fixture permanent failure")
                error = self._strict_translation_error(cause)
                self.assertIs(error.__cause__, cause)
                self.assertFalse(is_transient_network_error(error))

    def test_openai_deepseek_preserve_transport_and_http_failures(self):
        from backend.ai import translation

        for provider in (translation.translate_with_openai, translation.translate_with_deepseek):
            for status in (401, 429, 503):
                with self.subTest(provider=provider.__name__, status=status):
                    response = requests.Response()
                    response.status_code = status
                    failures = []
                    with patch.object(translation.requests, "post", return_value=response):
                        self.assertIsNone(provider(["你好"], api_key="fixture", failure_causes=failures))
                    self.assertTrue(failures)
                    self.assertEqual(failures[-1].response.status_code, status)
                    self.assertEqual(is_transient_network_error(failures[-1]), status != 401)
            failures = []
            with patch.object(translation.requests, "post", side_effect=requests.ConnectionError("connection failed")):
                self.assertIsNone(provider(["你好"], api_key="fixture", failure_causes=failures))
            self.assertIsInstance(failures[-1], requests.ConnectionError)
            self.assertTrue(is_transient_network_error(failures[-1]))

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / "bot_system").mkdir()
        self.source = self.root / "downloads" / "input.mp4"
        self.source.parent.mkdir()
        self.source.write_bytes(b"source fixture")
        self.job = {
            "type": "url", "url": "https://example.com/video", "pos": 1,
            "job_id": "telegram_network_retry", "voice_mode": "manual", "retry_count": 0,
            "job_overrides": {"voice_mode": "manual", "voice_config": {"manual_voice": {"id": "chosen"}},
                              "feature_flags": {"V1_VIDEO_MODE": "SHORT"}, "bgm_volume_db": -17},
            "resume_state": {"source_path": str(self.source), "source_sha256": "source-hash"},
        }
        for target, value in [
            ("WORKSPACE", str(self.root)), ("OUTPUT_DIR", str(self.root / "delivery")),
            ("global_queue", monitor.MonitoredQueue()), ("_network_retry_task", None),
            ("worker_task", SimpleNamespace(done=lambda: False)),
        ]:
            p = patch.object(bot, target, value)
            p.start()
            self.addCleanup(p.stop)
        bot._network_retry_jobs.clear()
        self.addCleanup(bot._network_retry_jobs.clear)
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
        batch_module = ModuleType("batch_processor")
        batch_module.BatchStopRequested = type("BatchStopRequested", (Exception,), {})
        p = patch.dict("sys.modules", {"batch_processor": batch_module})
        p.start()
        self.addCleanup(p.stop)
        p = patch.object(bot, "_ensure_network_retry_scheduler", MagicMock())
        p.start()
        self.addCleanup(p.stop)
        # Real intake freezes Phase 8 execution/idempotency settings before a
        # network failure can persist or retry the job.
        bot.prepare_telegram_job(self.job)

    def _retry_state(self):
        return json.loads(bot.network_retry_state_path().read_text(encoding="utf-8"))

    def _make_due(self):
        path = bot.network_retry_state_path()
        state = json.loads(path.read_text(encoding="utf-8"))
        for record in state["jobs"].values():
            record["next_attempt_at"] = 0
        path.write_text(json.dumps(state), encoding="utf-8")

    async def _run_failing_pipeline(self, error):
        from pipeline_v2 import download_validation

        with patch.object(download_validation, "probe_downloaded_video"), \
             patch("v1_checkpoint.fingerprint", return_value="source-hash"), \
             patch.object(bot, "run_pipeline_v1_for_telegram", side_effect=error), \
             patch.object(bot.job_tracker, "start_video"), \
             patch.object(bot.job_tracker, "set_error") as set_error, \
             patch.object(bot, "safe_edit_status", new_callable=AsyncMock) as status:
            result = await bot._process_telegram_job(copy.deepcopy(self.job))
        return result, set_error, status

    async def test_connect_error_is_persisted_with_backoff_and_not_failed(self):
        result, set_error, status = await self._run_failing_pipeline(
            httpx.ConnectError("All connection attempts failed")
        )
        self.assertEqual(result["status"], "retry_scheduled")
        self.assertEqual(bot.global_queue.qsize(), 0)
        set_error.assert_not_called()
        state = self._retry_state()
        record = state["jobs"]["telegram_network_retry"]
        self.assertEqual(record["attempt"], 1)
        self.assertEqual(record["delay_seconds"], 60)
        self.assertAlmostEqual(record["next_attempt_at"] - time.time(), 60, delta=5)
        self.assertEqual(record["job"]["job_id"], "telegram_network_retry")
        self.assertEqual(record["job"]["resume_state"]["source_sha256"], "source-hash")
        messages = [call.args[1] for call in status.call_args_list if len(call.args) > 1]
        self.assertTrue(any("Đang chờ mạng" in message and "thử lại" in message for message in messages))

    async def test_backoff_is_one_and_five_minutes_and_stops_after_two(self):
        expected = (60, 300)
        for attempt, delay in enumerate(expected, 1):
            scheduled = bot.schedule_network_retry(
                dict(copy.deepcopy(self.job), network_retry_count=attempt - 1)
            )
            self.assertEqual(scheduled["attempt"], attempt)
            self.assertEqual(scheduled["delay_seconds"], delay)
            self.assertAlmostEqual(
                self._retry_state()["jobs"]["telegram_network_retry"]["next_attempt_at"] - time.time(),
                delay, delta=5,
            )
        self.assertIsNone(
            bot.schedule_network_retry(dict(copy.deepcopy(self.job), network_retry_count=2))
        )
        self.assertEqual(len(self._retry_state()["jobs"]), 1)

    async def test_network_and_processing_errors_share_the_two_retry_limit(self):
        self.job["retry_count"] = 1
        result, _, _ = await self._run_failing_pipeline(httpx.ConnectError("offline"))
        self.assertEqual(result["status"], "retry_scheduled")
        self.assertIn("2/2", result["message"])
        self._make_due()
        self.assertEqual(await bot.process_due_network_retries(), 1)
        self.job = await bot.global_queue.get()
        bot.global_queue.task_done()
        self.assertEqual(self.job["network_retry_count"], 1)

        for error in (RuntimeError("TTS synthesis failed"), httpx.ConnectError("offline")):
            result, set_error, _ = await self._run_failing_pipeline(error)
            self.assertEqual(result["status"], "failed")
            set_error.assert_called_once()
            self.assertTrue(bot.global_queue.empty())
            self.assertEqual(self._retry_state()["jobs"], {})

    async def test_due_retry_is_enqueued_once_with_same_job_id(self):
        bot.schedule_network_retry(copy.deepcopy(self.job))
        self._make_due()
        self.assertEqual(await bot.process_due_network_retries(now=time.time()), 1)
        self.assertEqual(await bot.process_due_network_retries(now=time.time()), 0)
        self.assertEqual(bot.global_queue.qsize(), 1)
        queued = await bot.global_queue.get()
        self.assertEqual(queued["job_id"], "telegram_network_retry")
        self.assertEqual(queued["network_retry_count"], 1)
        self.assertEqual(queued["job_overrides"], self.job["job_overrides"])
        self.assertEqual(queued["resume_state"], self.job["resume_state"])
        self.assertEqual(self._retry_state()["jobs"], {})
        bot.global_queue.task_done()

    async def test_job_already_in_queue_is_not_duplicated(self):
        bot.schedule_network_retry(copy.deepcopy(self.job))
        await bot.global_queue.put(dict(copy.deepcopy(self.job), network_retry_count=1))
        self._make_due()
        self.assertEqual(await bot.process_due_network_retries(now=time.time()), 0)
        self.assertEqual(bot.global_queue.qsize(), 1)
        self.assertEqual(self._retry_state()["jobs"], {})
        await bot.global_queue.get()
        bot.global_queue.task_done()

    async def test_persisted_retry_survives_restart_with_identity_settings(self):
        bot.schedule_network_retry(copy.deepcopy(self.job))
        self._make_due()
        bot._network_retry_jobs.clear()
        restarted = monitor.MonitoredQueue()
        with patch.object(bot, "global_queue", restarted):
            self.assertEqual(await bot.process_due_network_retries(now=time.time()), 1)
            restored = await restarted.get()
        self.assertEqual(restored["job_id"], "telegram_network_retry")
        self.assertEqual(restored["network_retry_count"], 1)
        self.assertEqual(restored["resume_state"], self.job["resume_state"])
        self.assertEqual(restored["job_overrides"], self.job["job_overrides"])
        restarted.task_done()

    async def test_network_retries_are_capped_and_then_fail(self):
        job = copy.deepcopy(self.job)
        job["network_retry_count"] = 3
        from pipeline_v2 import download_validation

        with patch.object(download_validation, "probe_downloaded_video"), \
             patch("v1_checkpoint.fingerprint", return_value="source-hash"), \
             patch.object(bot, "run_pipeline_v1_for_telegram", side_effect=httpx.ConnectError("offline")), \
             patch.object(bot.job_tracker, "start_video"), \
             patch.object(bot.job_tracker, "set_error") as set_error, \
             patch.object(bot, "safe_edit_status", new_callable=AsyncMock) as status:
            result = await bot._process_telegram_job(job)
        self.assertEqual(result["status"], "failed")
        self.assertEqual(bot.global_queue.qsize(), 0)
        set_error.assert_called_once()
        path = bot.network_retry_state_path()
        retried = json.loads(path.read_text(encoding="utf-8"))["jobs"] if path.exists() else {}
        self.assertFalse(retried)
        messages = [call.args[1] for call in status.call_args_list if len(call.args) > 1]
        self.assertTrue(any("Không hoàn tất video" in message for message in messages))

    async def test_retry_only_restart_starts_one_worker_even_if_previous_worker_finished(self):
        for previous_finished in (False, True):
            with self.subTest(previous_finished=previous_finished):
                previous = None
                if previous_finished:
                    previous = asyncio.get_running_loop().create_future()
                    previous.set_result(None)
                consumed = asyncio.Event()
                keep_running = asyncio.Event()
                restored_jobs = []

                async def consume_retry():
                    restored_jobs.append(await bot.global_queue.get())
                    bot.global_queue.task_done()
                    consumed.set()
                    await keep_running.wait()

                queue_file = self.root / "bot_system" / "telegram_queue.json"
                queue_file.write_text(json.dumps({"items": [], "active_job": None}), encoding="utf-8")
                bot.schedule_network_retry(copy.deepcopy(self.job))
                bot._network_retry_jobs.clear()
                with patch.object(bot, "worker_task", previous), \
                     patch.object(bot, "video_worker", side_effect=consume_retry) as worker:
                    try:
                        self.assertEqual(await bot.enqueue_pending_queue_jobs(), 0)
                        self.assertIs(bot.worker_task, previous)
                        self._make_due()
                        self.assertEqual(await bot.process_due_network_retries(), 1)
                        await asyncio.wait_for(consumed.wait(), timeout=2)
                        started = bot.worker_task
                        self.assertIsNotNone(started)
                        self.assertIsNot(started, previous)
                        restored = restored_jobs[0]
                        self.assertEqual(restored["job_id"], self.job["job_id"])
                        self.assertEqual(restored["network_retry_count"], 1)
                        self.assertEqual(restored["job_overrides"], self.job["job_overrides"])
                        self.assertEqual(restored["resume_state"], self.job["resume_state"])

                        second = dict(copy.deepcopy(self.job), job_id="telegram_second_retry")
                        bot.schedule_network_retry(second)
                        self._make_due()
                        self.assertEqual(await bot.process_due_network_retries(), 1)
                        self.assertIs(bot.worker_task, started)
                        worker.assert_called_once()
                        self.assertEqual(await bot.process_due_network_retries(), 0)
                        self.assertEqual((await bot.global_queue.get())["job_id"], second["job_id"])
                        bot.global_queue.task_done()
                    finally:
                        if bot.worker_task is not None and bot.worker_task is not previous:
                            bot.worker_task.cancel()
                            await asyncio.gather(bot.worker_task, return_exceptions=True)

    async def _run_worker_failure(self, error, kind="url"):
        from pipeline_v2 import download_validation

        tracker = bot.job_tracker
        job = dict(copy.deepcopy(self.job), type=kind)
        if kind == "video":
            job.update(url="", file_id="fixture-file", filename="input.mp4")
        torch_stub = ModuleType("torch")
        torch_stub.cuda = SimpleNamespace(is_available=lambda: False)
        with patch.object(tracker, "WORKSPACE", self.root), \
             patch.object(tracker, "STATUS_FILE", self.root / "bot_system" / "job_status.json"), \
             patch.object(tracker, "BATCH_LOCK_FILE", self.root / "bot_system" / "batch.lock"), \
             patch.object(tracker, "_CURRENT_STATE", copy.deepcopy(tracker._DEFAULT_STATE)), \
             patch.dict("sys.modules", {"job_tracker": tracker, "torch": torch_stub}), \
             patch.object(download_validation, "probe_downloaded_video"), \
             patch("v1_checkpoint.fingerprint", return_value="source-hash"), \
             patch.object(bot, "run_pipeline_v1_for_telegram", side_effect=error), \
             patch.object(bot, "safe_edit_status", new_callable=AsyncMock), \
             patch.object(tracker, "fail_batch", wraps=tracker.fail_batch) as fail, \
             patch.object(tracker, "finish_batch", wraps=tracker.finish_batch) as finish:
            await bot.global_queue.put(job)
            task = asyncio.create_task(bot.video_worker())
            try:
                await asyncio.wait_for(bot.global_queue.join(), timeout=5)
                state = tracker.get_status()
                disk = json.loads(tracker.STATUS_FILE.read_text(encoding="utf-8"))
                self.assertFalse(tracker.BATCH_LOCK_FILE.exists())
                self.assertIsNone(bot.global_queue.active_job)
                self.assertEqual(bot.global_queue._unfinished_tasks, 0)
                finish.assert_not_called()
                fail.assert_not_called()
                return state, disk
            finally:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)

    async def _assert_worker_waits_for_network(self, kind):
        state, disk = await self._run_worker_failure(httpx.ConnectError("offline"), kind)
        for snapshot in (state, disk):
            self.assertEqual(snapshot["status"], "waiting")
            self.assertEqual(snapshot["video_status"], "waiting")
            self.assertFalse(snapshot["active"])
            self.assertIsNone(snapshot["last_error"])
            self.assertIsNone(snapshot["eta_seconds"])
            self.assertLess(snapshot["percent"], 100)
            self.assertEqual(snapshot["history"], [])
            self.assertIn("thử lại", snapshot["step_name"])
            self.assertEqual(snapshot["workers"]["1"]["status"], "waiting")
            self.assertFalse(snapshot["workers"]["1"]["active"])
        retry = self._retry_state()["jobs"][self.job["job_id"]]
        self.assertEqual(retry["attempt"], 1)
        self.assertEqual(retry["delay_seconds"], 60)

    async def test_url_worker_waits_for_network_without_failing_or_finishing(self):
        await self._assert_worker_waits_for_network("url")

    async def test_video_worker_waits_for_network_without_failing_or_finishing(self):
        await self._assert_worker_waits_for_network("video")

    async def test_worker_still_fails_permanent_download_error_without_retry(self):
        state, disk = await self._run_worker_failure(
            DownloadValidationError("ffprobe rejected the downloaded container")
        )
        for snapshot in (state, disk):
            self.assertEqual(snapshot["status"], "error")
            self.assertEqual(snapshot["video_status"], "error")
            self.assertFalse(snapshot["active"])
            self.assertIn("ffprobe rejected", snapshot["last_error"])
        self.assertFalse(bot.network_retry_state_path().exists())

    async def test_worker_still_fails_when_network_retries_are_exhausted(self):
        self.job["network_retry_count"] = 3
        state, disk = await self._run_worker_failure(httpx.ConnectError("offline"))
        for snapshot in (state, disk):
            self.assertEqual(snapshot["status"], "error")
            self.assertEqual(snapshot["video_status"], "error")
            self.assertFalse(snapshot["active"])
            self.assertIn("offline", snapshot["last_error"])
        self.assertFalse(bot.network_retry_state_path().exists())

    async def test_corrupt_video_fails_immediately_without_retry(self):
        result, set_error, status = await self._run_failing_pipeline(
            DownloadValidationError("ffprobe rejected the downloaded container")
        )
        self.assertEqual(result["status"], "failed")
        self.assertEqual(bot.global_queue.qsize(), 0)
        set_error.assert_called_once()
        self.assertFalse(bot.network_retry_state_path().exists())
        messages = [call.args[1] for call in status.call_args_list if len(call.args) > 1]
        self.assertTrue(any("Không hoàn tất video" in message for message in messages))

    def test_downloader_connection_strings_are_network_errors(self):
        self.assertTrue(is_transient_network_error(httpx.ConnectError("refused")))
        self.assertTrue(is_transient_network_error(
            RuntimeError("Không thể kết nối đến máy chủ Tiểu Hồng Thư (Hết thời gian chờ hoặc bị chặn)")
        ))
        self.assertTrue(is_transient_network_error(
            RuntimeError("Bóc tách Douyin thất bại: Direct: Kết nối API Douyin thất bại: ConnectError")
        ))
        wrapped = RuntimeError("worker failed")
        wrapped.__cause__ = ConnectionError("getaddrinfo failed")
        self.assertTrue(is_transient_network_error(wrapped))
        self.assertFalse(is_transient_network_error(RuntimeError("Downloaded file is missing or empty")))
        self.assertFalse(is_transient_network_error(
            RuntimeError("Bài viết trên Tiểu Hồng Thư này đã bị tác giả xóa (Trang không tồn tại)")
        ))
