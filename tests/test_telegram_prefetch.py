import asyncio
import json
import os
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import pytest

pytest.importorskip("telegram")

from backend import telegram_bot as bot
from backend import telegram_queue_monitor as monitor


class TelegramPrefetchTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / "bot_system").mkdir()
        self.queue = monitor.MonitoredQueue()
        self.shared_state = SimpleNamespace(stop_requested=False)
        patchers = [
            patch.object(bot, "global_queue", self.queue),
            patch.object(bot, "WORKSPACE", str(self.root)),
            patch.object(bot, "OUTPUT_DIR", str(self.root / "output")),
            patch.object(bot, "GLOBAL_BOT_APP", None),
            patch.object(bot, "worker_task", None),
            patch.object(bot, "shared_state", self.shared_state),
            patch.object(bot, "_prefetch_tasks", set()),
            patch.object(monitor, "SNAPSHOT", self.root / "bot_system" / "telegram_queue.json"),
            patch.dict(os.environ, {
                "V1_TELEGRAM_PREFETCH": "1",
                "V1_TELEGRAM_PREFETCH_DEPTH": "1",
            }),
            patch.dict("sys.modules", {"shared_state": self.shared_state}),
        ]
        for patcher in patchers:
            patcher.start()
            self.addCleanup(patcher.stop)

    @staticmethod
    def make_job(job_id="job-1", *, job_type="url", execution_mode="local"):
        return {
            "type": job_type,
            "job_id": job_id,
            "execution_mode": execution_mode,
            "url": f"https://example.invalid/{job_id}",
            "file_id": "telegram-file",
            "filename": f"{job_id}.mp4",
            "pos": 1,
            "resume_state": {},
        }

    async def fake_download(self, job, status_msg=None, *, reload_downloader=True):
        source = self.root / "downloads" / f"{job['job_id']}.mp4"
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_bytes(b"prefetched video")
        job["resume_state"].update(source_path=str(source), source_sha256="source-hash")
        return source

    async def test_prefetch_is_reused_by_worker_without_duplicate_download(self):
        job = self.make_job()
        self.queue.put_nowait(job)
        download = AsyncMock(side_effect=self.fake_download)
        source = self.root / "output" / "Dubbed_job-1.mp4"

        with patch.object(bot, "_download_job_source", download), \
             patch.object(bot, "prepare_telegram_job", side_effect=lambda item: item), \
             patch("pipeline_v2.download_validation.probe_downloaded_video"), \
             patch("v1_checkpoint.fingerprint", return_value="source-hash"), \
             patch.object(bot, "run_pipeline_v1_for_telegram", AsyncMock(return_value={
                 "status": "success",
                 "final_video": str(source),
                 "tracker_finalized": True,
             })), \
             patch.object(bot.job_tracker, "start_video"), \
             patch.object(bot, "safe_edit_status", AsyncMock()):
            task = bot._schedule_next_download_prefetch()
            self.assertIsNotNone(task)
            self.queue.publish()
            await task
            snapshot = json.loads(monitor.SNAPSHOT.read_text(encoding="utf-8"))
            self.assertNotIn("_prefetch_task", snapshot["items"][0])
            result = await bot._process_telegram_job(job)

        self.assertEqual(result["status"], "success")
        download.assert_awaited_once()
        self.assertFalse(download.await_args.kwargs["reload_downloader"])

    async def test_prefetch_finishing_after_prepare_is_reused_without_duplicate_download(self):
        job = self.make_job()
        await self.queue.put(job)
        source = self.root / "downloads" / "job-1.mp4"
        download_started = threading.Event()
        finish_download = threading.Event()
        downloads = []

        def blocking_download(_url, output_dir, job_id):
            downloads.append(job_id)
            download_started.set()
            finish_download.wait(timeout=3)
            path = Path(output_dir) / f"{job_id}.mp4"
            path.write_bytes(b"prefetched video")
            return True, str(path), "Title", None

        prepared = asyncio.Event()

        def prepare(item):
            item["resume_state"] = dict(item.get("resume_state") or {})
            prepared.set()
            return item

        with patch.dict("sys.modules", {
            "social_downloader": SimpleNamespace(download_social_video=blocking_download)
        }), patch.object(bot, "prepare_telegram_job", side_effect=prepare), \
             patch("pipeline_v2.download_validation.probe_downloaded_video"), \
             patch("v1_checkpoint.fingerprint", return_value="source-hash"), \
             patch.object(bot, "run_pipeline_v1_for_telegram", AsyncMock(return_value={
                 "status": "success",
                 "final_video": str(self.root / "output" / "Dubbed_job-1.mp4"),
                 "tracker_finalized": True,
             })), \
             patch.object(bot.job_tracker, "start_video"), \
             patch.object(bot, "safe_edit_status", AsyncMock()):
            task = bot._schedule_next_download_prefetch()
            await asyncio.wait_for(asyncio.to_thread(download_started.wait, 2), timeout=3)
            worker = asyncio.create_task(bot._process_telegram_job(job))
            await prepared.wait()
            finish_download.set()
            result = await worker

        self.assertEqual(result["status"], "success")
        self.assertEqual(downloads, ["job-1"])
        self.assertEqual(job["resume_state"]["source_path"], str(source))
        self.assertEqual(job["resume_state"]["title"], "Title")

    async def test_prefetch_depth_limits_which_waiting_jobs_are_considered(self):
        cloud_job = self.make_job("cloud", execution_mode="cloud")
        local_job = self.make_job("local")
        await self.queue.put(cloud_job)
        await self.queue.put(local_job)
        download = AsyncMock(side_effect=self.fake_download)

        with patch.object(bot, "_download_job_source", download):
            self.assertIsNone(bot._schedule_next_download_prefetch())
            os.environ["V1_TELEGRAM_PREFETCH_DEPTH"] = "2"
            task = bot._schedule_next_download_prefetch()
            self.assertIsNotNone(task)
            await task

        download.assert_awaited_once()
        self.assertIs(download.await_args.args[0], local_job)

    async def test_only_one_prefetch_runs_at_a_time(self):
        first = self.make_job("first")
        second = self.make_job("second")
        await self.queue.put(first)
        await self.queue.put(second)
        started = asyncio.Event()
        release = asyncio.Event()
        calls = []

        async def wait_for_release(job, status_msg=None, *, reload_downloader=True):
            calls.append(job)
            started.set()
            await release.wait()
            return self.root / f"{job['job_id']}.mp4"

        with patch.object(bot, "_download_job_source", side_effect=wait_for_release):
            os.environ["V1_TELEGRAM_PREFETCH_DEPTH"] = "2"
            first_task = bot._schedule_next_download_prefetch()
            await started.wait()
            self.assertIsNone(bot._schedule_next_download_prefetch())
            release.set()
            await first_task
            second_task = bot._schedule_next_download_prefetch()
            self.assertIsNotNone(second_task)
            await second_task

        self.assertEqual(calls, [first, second])

    async def test_cloud_and_local_file_jobs_are_skipped(self):
        await self.queue.put(self.make_job("cloud", execution_mode="cloud"))
        await self.queue.put(self.make_job("local-file", job_type="local_file"))
        download = AsyncMock()

        with patch.object(bot, "_download_job_source", download):
            os.environ["V1_TELEGRAM_PREFETCH_DEPTH"] = "2"
            self.assertIsNone(bot._schedule_next_download_prefetch())

        download.assert_not_awaited()

    async def test_url_download_helper_preserves_probe_hash_and_title(self):
        job = self.make_job()
        source = self.root / "downloads" / "job-1.mp4"
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_bytes(b"downloaded video")
        downloader = Mock(return_value=(True, str(source), "Title", None))
        social_downloader = SimpleNamespace(download_social_video=downloader)

        with patch.dict("sys.modules", {"social_downloader": social_downloader}), \
             patch("importlib.reload", side_effect=lambda module: module) as reload_module, \
             patch("pipeline_v2.download_validation.probe_downloaded_video") as probe, \
             patch("v1_checkpoint.fingerprint", return_value="source-hash"), \
             patch.object(bot, "safe_edit_status", AsyncMock()):
            result = await bot._download_job_source(job)

        self.assertEqual(result, source)
        self.assertEqual(job["resume_state"]["source_path"], str(source))
        self.assertEqual(job["resume_state"]["source_sha256"], "source-hash")
        self.assertEqual(job["resume_state"]["title"], "Title")
        downloader.assert_called_once_with(job["url"], str(source.parent), job["job_id"])
        reload_module.assert_called_once_with(social_downloader)
        probe.assert_called_once_with(source)

    async def test_telegram_source_download_promotes_partial_file(self):
        class TelegramFile:
            async def download_to_drive(self, path, **kwargs):
                Path(path).write_bytes(b"downloaded video")

        class TelegramBot:
            async def get_file(self, *args, **kwargs):
                return TelegramFile()

        job = self.make_job(job_type="video")
        job["context"] = SimpleNamespace(bot=TelegramBot())
        final_path = self.root / "downloads" / "job-1.mp4"
        partial_path = final_path.with_suffix(".partial.mp4")
        with patch("pipeline_v2.download_validation.probe_downloaded_video") as probe, \
             patch("v1_checkpoint.fingerprint", return_value="source-hash"), \
             patch.object(bot, "safe_edit_status", AsyncMock()):
            result = await bot._download_job_source(job)

        self.assertEqual(result, final_path)
        self.assertTrue(final_path.is_file())
        self.assertFalse(partial_path.exists())
        self.assertEqual(job["resume_state"]["source_path"], str(final_path))
        self.assertEqual(job["resume_state"]["source_sha256"], "source-hash")
        probe.assert_called_once_with(partial_path)

    async def test_prefetch_failure_falls_back_to_worker_download(self):
        job = self.make_job()
        await self.queue.put(job)
        calls = []

        async def fail_then_download(item, status_msg=None, *, reload_downloader=True):
            calls.append(reload_downloader)
            if len(calls) == 1:
                raise RuntimeError("prefetch failed")
            return await self.fake_download(item, status_msg, reload_downloader=reload_downloader)

        with patch.object(bot, "_download_job_source", side_effect=fail_then_download), \
             patch.object(bot, "prepare_telegram_job", side_effect=lambda item: item), \
             patch("pipeline_v2.download_validation.probe_downloaded_video"), \
             patch("v1_checkpoint.fingerprint", return_value="source-hash"), \
             patch.object(bot, "run_pipeline_v1_for_telegram", AsyncMock(return_value={
                 "status": "success",
                 "final_video": str(self.root / "output" / "Dubbed_job-1.mp4"),
                 "tracker_finalized": True,
             })), \
             patch.object(bot.job_tracker, "start_video"), \
             patch.object(bot, "safe_edit_status", AsyncMock()):
            task = bot._schedule_next_download_prefetch()
            with self.assertRaisesRegex(RuntimeError, "prefetch failed"):
                await task
            result = await bot._process_telegram_job(job)

        self.assertEqual(result["status"], "success")
        self.assertEqual(calls, [False, True])

    async def test_cancelled_prefetch_falls_back_without_task_cancelling_api(self):
        job = self.make_job()
        await self.queue.put(job)
        calls = []

        async def cancel_then_download(item, status_msg=None, *, reload_downloader=True):
            calls.append(reload_downloader)
            if not reload_downloader:
                raise asyncio.CancelledError
            return await self.fake_download(item, status_msg, reload_downloader=reload_downloader)

        with patch.object(bot, "_download_job_source", side_effect=cancel_then_download), \
             patch.object(bot, "prepare_telegram_job", side_effect=lambda item: item), \
             patch("pipeline_v2.download_validation.probe_downloaded_video"), \
             patch("v1_checkpoint.fingerprint", return_value="source-hash"), \
             patch.object(bot, "run_pipeline_v1_for_telegram", AsyncMock(return_value={
                 "status": "success",
                 "final_video": str(self.root / "output" / "Dubbed_job-1.mp4"),
                 "tracker_finalized": True,
             })), \
             patch.object(bot.job_tracker, "start_video"), \
             patch.object(bot, "safe_edit_status", AsyncMock()):
            task = bot._schedule_next_download_prefetch()
            with self.assertRaises(asyncio.CancelledError):
                await task
            with patch.object(bot.asyncio, "current_task", return_value=SimpleNamespace()):
                result = await bot._process_telegram_job(job)

        self.assertEqual(result["status"], "success")
        self.assertEqual(calls, [False, True])

    async def test_cancelled_url_prefetch_removes_download_when_thread_finishes(self):
        job = self.make_job()
        await self.queue.put(job)
        download_started = threading.Event()
        finish_download = threading.Event()
        download_finished = threading.Event()
        source = self.root / "downloads" / "job-1.mp4"

        def blocking_download(_url, output_dir, job_id):
            download_started.set()
            finish_download.wait(timeout=3)
            path = Path(output_dir) / f"{job_id}.mp4"
            path.write_bytes(b"completed after cancellation")
            download_finished.set()
            return True, str(path), "Title", None

        with patch.dict("sys.modules", {
            "social_downloader": SimpleNamespace(download_social_video=blocking_download)
        }), patch.object(bot, "safe_edit_status", AsyncMock()):
            task = bot._schedule_next_download_prefetch()
            await asyncio.wait_for(asyncio.to_thread(download_started.wait, 2), timeout=3)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
            finish_download.set()
            await asyncio.wait_for(asyncio.to_thread(download_finished.wait, 2), timeout=3)
            for _ in range(100):
                if not source.exists():
                    break
                await asyncio.sleep(0.01)

        self.assertFalse(source.exists())

    async def test_stop_cancels_prefetch_and_removes_partial_telegram_download(self):
        started = asyncio.Event()

        class TelegramFile:
            async def download_to_drive(self, path, **kwargs):
                Path(path).write_bytes(b"partial")
                started.set()
                await asyncio.Future()

        class TelegramBot:
            async def get_file(self, *args, **kwargs):
                return TelegramFile()

        job = self.make_job(job_type="video")
        job["context"] = SimpleNamespace(bot=TelegramBot())
        await self.queue.put(job)
        with patch.object(bot, "safe_edit_status", AsyncMock()), \
             patch.object(bot.job_tracker, "request_stop"), \
             patch.object(bot, "clear_network_retries"), \
             patch.dict("sys.modules", {
                 "psutil": SimpleNamespace(
                     Process=lambda _pid: SimpleNamespace(children=lambda recursive: [])
                 )
             }):
            task = bot._schedule_next_download_prefetch()
            partial = self.root / "downloads" / "job-1.partial.mp4"
            await asyncio.wait_for(started.wait(), timeout=2)
            self.assertTrue(partial.is_file())
            update = SimpleNamespace(message=SimpleNamespace(reply_text=AsyncMock()))
            await bot.cmd_stop(update, None)
            with self.assertRaises(asyncio.CancelledError):
                await task

        self.assertFalse(partial.exists())
        self.assertTrue(self.queue.empty())

    async def test_environment_flag_disables_prefetch(self):
        await self.queue.put(self.make_job())
        with patch.dict(os.environ, {"V1_TELEGRAM_PREFETCH": "0"}):
            self.assertIsNone(bot._schedule_next_download_prefetch())
            await bot.download_prefetcher()


if __name__ == "__main__":
    unittest.main()
