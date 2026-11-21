"""Offline regression tests for dedicated Cloud Telegram Bot and D:\\ToolV1_Cloud output."""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from backend.cloud import cloud_config, cloud_job_store, cloud_security, cloud_telegram_bot
from backend.cloud.cloud_contract import admit_and_enqueue_job
from backend.cloud.cloud_job_store import CloudJobStore
from backend.cloud.cloud_telegram_bot import (
    TELEGRAM_BOT_DOWNLOAD_LIMIT_BYTES,
    CloudBotQueuedItem,
    CloudBotStateFiles,
    CloudTelegramBotService,
    extract_video_urls,
    resolve_voice_choice,
    validate_cloud_bot_gpu,
)
import run_cloud_node as node


SYNTHETIC_BOT_TOKEN = "1234567890:AAFakeSyntheticTestToken0123456789ab"
MP4_HEADER = b"\x00\x00\x00\x18ftypmp42\x00\x00\x00\x00mp42isom" + b"\x00" * 64


def _make_update(user_id: int = 777001, text: str = "", chat_id: int = 777001, video=None):
    replies = []
    edits = []

    async def _edit_text(new_text, **kwargs):
        edits.append(new_text)

    async def _reply_text(msg_text, **kwargs):
        status_obj = SimpleNamespace(edit_text=AsyncMock(side_effect=_edit_text))
        replies.append((msg_text, status_obj))
        return status_obj

    message = SimpleNamespace(
        chat_id=chat_id,
        text=text,
        video=video,
        document=None,
        reply_text=AsyncMock(side_effect=_reply_text),
    )
    user = SimpleNamespace(id=user_id, username="owner_test")
    update = SimpleNamespace(
        effective_user=user,
        effective_message=message,
        message=message,
    )
    return update, replies, edits


def test_cloud_output_dir_defaults_and_override(monkeypatch, tmp_path):
    monkeypatch.delenv("AUTODUB_CLOUD_OUTPUT_DIR", raising=False)
    monkeypatch.delenv("AUTODUB_OUTPUT_DIR", raising=False)
    assert str(cloud_config.get_cloud_output_dir()) == r"D:\ToolV1_Cloud"

    # Even when Local V1's .env sets AUTODUB_OUTPUT_DIR=D:\banve, Cloud stays isolated in D:\ToolV1_Cloud!
    monkeypatch.setenv("AUTODUB_OUTPUT_DIR", r"D:\banve")
    assert str(cloud_config.get_cloud_output_dir()) == r"D:\ToolV1_Cloud"

    local_dir = tmp_path / "local_out"
    cloud_dir = tmp_path / "cloud_out"
    monkeypatch.setenv("AUTODUB_OUTPUT_DIR", str(local_dir))
    monkeypatch.setenv("AUTODUB_CLOUD_OUTPUT_DIR", str(cloud_dir))
    assert cloud_config.get_cloud_output_dir() == cloud_dir
    assert cloud_dir.resolve() in cloud_security.get_default_allowed_roots()


def test_cloud_bot_token_resolution_and_redaction(monkeypatch, tmp_path):
    ws = tmp_path / "ws"
    (ws / "cloud_desktop").mkdir(parents=True)
    monkeypatch.setenv("AUTODUB_WORKSPACE", str(ws))
    monkeypatch.delenv("CLOUD_BOT_TOKEN", raising=False)
    assert cloud_security.resolve_cloud_bot_token() == ""

    token_file = ws / "cloud_desktop" / ".cloud_bot_token"
    token_file.write_text(SYNTHETIC_BOT_TOKEN + "\n", encoding="utf-8")
    assert cloud_security.resolve_cloud_bot_token() == SYNTHETIC_BOT_TOKEN

    leaked = f"POST https://api.telegram.org/bot{SYNTHETIC_BOT_TOKEN}/getMe failed"
    scrubbed = cloud_security.redact_secrets(leaked)
    assert SYNTHETIC_BOT_TOKEN not in scrubbed
    assert "AAFakeSynthetic" not in scrubbed


def test_owner_auto_pair_locks_to_first_user_and_blocks_strangers(monkeypatch, tmp_path):
    monkeypatch.delenv("CLOUD_BOT_ALLOWED_USER_IDS", raising=False)
    state = CloudBotStateFiles(tmp_path / "ws")

    allowed_first, newly_paired = state.authorize_user(10001, "alice")
    assert allowed_first is True and newly_paired is True

    allowed_again, newly_again = state.authorize_user(10001, "alice")
    assert allowed_again is True and newly_again is False

    allowed_stranger, _ = state.authorize_user(99999, "mallory")
    assert allowed_stranger is False


def test_commands_gpu_voice_budget_and_tpu_rejection(monkeypatch, tmp_path):
    monkeypatch.delenv("CLOUD_BOT_ALLOWED_USER_IDS", raising=False)
    ws = tmp_path / "ws"
    monkeypatch.setenv("AUTODUB_WORKSPACE", str(ws))
    store = CloudJobStore(db_path=ws / "cloud_jobs.db", rollout_controlled=False)
    svc = CloudTelegramBotService(workspace_dir=ws, store=store, adapter_factory=Mock)

    assert validate_cloud_bot_gpu("l4") == "L4"
    for accelerator in ("CPU", "V5E1", "V6E1"):
        with pytest.raises(cloud_security.CloudValidationError, match="CUDA"):
            validate_cloud_bot_gpu(accelerator)
    assert resolve_voice_choice("nam") == "BV075_streaming"

    async def exercise():
        upd, replies, _ = _make_update(user_id=501)
        await svc.cmd_gpu(upd, SimpleNamespace(args=["L4"]))
        assert svc.get_status()["default_gpu"] == "L4"
        assert any("NVIDIA L4" in text for text, _ in replies)

        upd_tpu, replies_tpu, _ = _make_update(user_id=501)
        await svc.cmd_gpu(upd_tpu, SimpleNamespace(args=["V6E1"]))
        assert svc.get_status()["default_gpu"] == "L4"
        assert any("CUDA" in text for text, _ in replies_tpu)

        upd_voice, _, _ = _make_update(user_id=501)
        await svc.cmd_voice(upd_voice, SimpleNamespace(args=["hoaimy"]))
        assert svc.state_files.get_prefs()["voice"] == "vi-VN-HoaiMyNeural"

        upd_budget, _, _ = _make_update(user_id=501)
        await svc.cmd_budget(upd_budget, SimpleNamespace(args=["14.5"]))
        assert store.get_cu_budget_info()["budget_daily"] == pytest.approx(14.5)

        stranger_upd, stranger_replies, _ = _make_update(user_id=999)
        await svc.cmd_budget(stranger_upd, SimpleNamespace(args=["100"]))
        assert store.get_cu_budget_info()["budget_daily"] == pytest.approx(14.5)
        assert any("không có quyền" in text for text, _ in stranger_replies)

        concurrency_upd, concurrency_replies, _ = _make_update(user_id=501)
        await svc.cmd_concurrency(concurrency_upd, SimpleNamespace(args=["3"]))
        assert store.get_concurrency_limit() == 3
        assert any("tối đa `3`" in text for text, _ in concurrency_replies)

        invalid_upd, invalid_replies, _ = _make_update(user_id=501)
        await svc.cmd_concurrency(invalid_upd, SimpleNamespace(args=["5"]))
        assert store.get_concurrency_limit() == 3
        assert any("không hợp lệ" in text for text, _ in invalid_replies)

    asyncio.run(exercise())


def test_telegram_consumer_runs_only_configured_number_of_items(monkeypatch, tmp_path):
    ws = tmp_path / "ws"
    store = CloudJobStore(db_path=ws / "cloud_jobs.db", rollout_controlled=False)
    store.set_concurrency_limit(2)
    svc = CloudTelegramBotService(workspace_dir=ws, store=store, adapter_factory=Mock)
    svc._queue = asyncio.Queue()
    items = [
        CloudBotQueuedItem(
            item_id=f"queued-{i}", kind="url", chat_id=777, source_label=f"item-{i}",
        )
        for i in range(3)
    ]
    for item in items:
        svc._pending_items.append(item)
        svc._queue.put_nowait(item)

    started = asyncio.Event()
    release = asyncio.Event()
    active = 0
    max_active = 0
    processed = []

    async def fake_process(_bot, item):
        nonlocal active, max_active
        active += 1
        max_active = max(max_active, active)
        processed.append(item.item_id)
        if len(processed) >= 2:
            started.set()
        await release.wait()
        active -= 1

    monkeypatch.setattr(svc, "_process_item", fake_process)

    async def exercise():
        stop_ev = asyncio.Event()
        consumer = asyncio.create_task(
            svc._queue_consumer_loop(SimpleNamespace(bot=SimpleNamespace()), stop_ev)
        )
        await asyncio.wait_for(started.wait(), timeout=2.0)
        assert max_active == 2
        assert len(processed) == 2
        release.set()
        for _ in range(100):
            if len(processed) == 3:
                break
            await asyncio.sleep(0.01)
        assert len(processed) == 3
        stop_ev.set()
        await asyncio.wait_for(consumer, timeout=2.0)
        assert max_active == 2
        assert svc._queue._unfinished_tasks == 0

    asyncio.run(exercise())


@pytest.mark.parametrize("miss_precheck", [False, True])
def test_server_channel_overrides_options_and_blocks_cross_channel_replay(monkeypatch, tmp_path, miss_precheck):
    store = CloudJobStore(db_path=tmp_path / "cloud_jobs.db", rollout_controlled=False)
    video = tmp_path / "sample.mp4"
    video.write_bytes(MP4_HEADER)
    request = {
        "video_path": str(video),
        "allowed_roots": [tmp_path],
        "idempotency_key": "cloudbot:dashboard-key",
    }
    dashboard, contract = admit_and_enqueue_job(
        store, **request, source_channel="dashboard", options={"source_channel": "cloud_telegram_bot"}
    )
    assert dashboard.options["source_channel"] == contract.source_channel == "dashboard"
    assert contract.options_snapshot.extra_options["source_channel"] == "dashboard"
    retry, retry_contract = admit_and_enqueue_job(store, **request, source_channel="dashboard")
    assert retry.job_id == dashboard.job_id and retry_contract.deduplicated

    if miss_precheck:
        # Another writer can insert the key after the lookup and before enqueue_or_get.
        monkeypatch.setattr(store, "get_job_by_idempotency_key", Mock(return_value=None))
    with pytest.raises(cloud_security.CloudValidationError) as exc:
        admit_and_enqueue_job(store, **request, source_channel="telegram")
    assert exc.value.http_status == 409

    telegram, contract = admit_and_enqueue_job(
        store,
        video_path=str(video),
        allowed_roots=[tmp_path],
        source_channel="Telegram",
        options={"source_channel": "dashboard"},
    )
    assert telegram.options["source_channel"] == "cloud_telegram_bot"
    assert contract.options_snapshot.extra_options["source_channel"] == "cloud_telegram_bot"
    assert contract.source_channel == "Telegram"  # Keep the pre-existing wire label/alias.


@pytest.mark.parametrize("action", ["stop", "restart"])
def test_telegram_owned_job_filter_preserves_conflicting_dashboard_rows(monkeypatch, tmp_path, action):
    ws = tmp_path / "ws"
    store = CloudJobStore(db_path=ws / "cloud_jobs.db", rollout_controlled=False)
    svc = CloudTelegramBotService(workspace_dir=ws, store=store, adapter_factory=Mock)
    cancel_worker = Mock()
    monkeypatch.setattr(svc.worker, "cancel_job", cancel_worker)
    video = ws / "sample.mp4"
    video.write_bytes(MP4_HEADER)
    telegram = store.enqueue(str(video), options={"source_channel": "telegram"})
    legacy = store.enqueue(str(video), idempotency_key="cloudbot:legacy")
    queued_dashboard = store.enqueue(
        str(video), idempotency_key="cloudbot:dashboard-queued", options={"source_channel": "dashboard"}
    )
    active_dashboard = store.enqueue(
        str(video), idempotency_key="cloudbot:dashboard-active", options={"source_channel": "dashboard"}
    )
    assert store.claim_job(active_dashboard.job_id, owner_id="dashboard-worker") is not None
    assert {job.job_id for job in svc._telegram_jobs()} == {telegram.job_id, legacy.job_id}

    if action == "restart":
        assert svc._cancel_abandoned_queued_jobs_sync() == 2
    else:
        update, replies, _ = _make_update(user_id=777, text="/stop")
        asyncio.run(svc.cmd_stop(update, SimpleNamespace()))
        assert any("Máy ảo đang dùng bởi tác vụ khác" in text for text, _ in replies)

    assert store.get_job(telegram.job_id).state == "cancelled"
    assert store.get_job(legacy.job_id).state == "cancelled"
    assert store.get_job(queued_dashboard.job_id).state == "queued"
    active = store.get_job(active_dashboard.job_id)
    assert (active.state, active.owner_id, active.cancel_requested_at) == ("provisioning", "dashboard-worker", None)
    cancel_worker.assert_not_called()


@pytest.mark.parametrize("notice_fails", [False, True])
def test_url_submission_auto_runs_exports_to_cloud_dir_and_notifies_by_text(monkeypatch, tmp_path, notice_fails):
    monkeypatch.delenv("CLOUD_BOT_ALLOWED_USER_IDS", raising=False)
    ws = tmp_path / "ws"
    cloud_out = tmp_path / "ToolV1_Cloud"
    monkeypatch.setenv("AUTODUB_WORKSPACE", str(ws))
    monkeypatch.setenv("AUTODUB_CLOUD_OUTPUT_DIR", str(cloud_out))

    store = CloudJobStore(db_path=ws / "cloud_jobs.db", rollout_controlled=False)

    def fake_downloader(url: str, dest_dir: str, tag: str):
        dl_path = Path(dest_dir) / f"raw_{tag}.mp4"
        dl_path.write_bytes(MP4_HEADER)
        return True, str(dl_path), "Clip Douyin Test", ""

    svc = CloudTelegramBotService(
        workspace_dir=ws,
        store=store,
        downloader_fn=fake_downloader,
        adapter_factory=Mock,
    )

    def fake_process_claimed(job, progress_cb=None):
        if progress_cb:
            progress_cb(50, "Whisper Large-v3 đang nhận dạng...")
        cloud_out.mkdir(parents=True, exist_ok=True)
        out_video = cloud_out / f"cloud_result_{job.job_id}.mp4"
        out_video.write_bytes(MP4_HEADER)
        for st in ("uploading", "processing", "downloading", "validating"):
            assert store.update_progress(
                job.job_id,
                state=st,
                percent=60,
                step_name=st,
                owner_id=svc.worker.worker_id,
                attempt=job.attempt,
            )
        assert store.update_progress(
            job.job_id,
            state="completed",
            percent=100,
            step_name="Hoàn thành!",
            result_path=str(out_video),
            cu_actual=0.18,
            owner_id=svc.worker.worker_id,
            attempt=job.attempt,
            cleanup_status="verified",
        )
        return True

    monkeypatch.setattr(svc.worker, "process_claimed_job", fake_process_claimed)

    fake_bot = SimpleNamespace(
        send_message=AsyncMock(side_effect=RuntimeError("Telegram unavailable") if notice_fails else None),
        send_video=AsyncMock(),
        send_document=AsyncMock(),
    )

    async def exercise():
        svc._queue = asyncio.Queue()
        upd, replies, edits = _make_update(
            user_id=777,
            text="Xem clip này https://www.tiktok.com/@demo/video/1234567890!",
        )
        await svc.handle_text_message(upd, SimpleNamespace())
        assert svc._queue.qsize() == 1
        queued_item = await svc._queue.get()
        await svc._process_item(fake_bot, queued_item)

        result = store.get_job(queued_item.cloud_job_id)
        result_file = cloud_out / f"cloud_result_{result.job_id}.mp4"
        assert result.result_path == str(result_file)
        assert result_file.read_bytes() == MP4_HEADER
        fake_bot.send_message.assert_awaited_once()
        notice = fake_bot.send_message.await_args.kwargs
        assert notice["chat_id"] == queued_item.chat_id
        assert notice["parse_mode"] is None
        assert "Hoàn tất Cloud GPU" in notice["text"]
        assert str(result_file) in notice["text"]
        assert (notice["text"] in edits) == notice_fails
        fake_bot.send_video.assert_not_called()
        fake_bot.send_document.assert_not_called()
        # Temporary downloaded source must be cleaned up after completion.
        assert list(svc.state_files.downloads_dir.glob("*.mp4")) == []

    asyncio.run(exercise())


def test_daily_budget_exceeded_blocks_before_download_or_gpu(monkeypatch, tmp_path):
    monkeypatch.delenv("CLOUD_BOT_ALLOWED_USER_IDS", raising=False)
    ws = tmp_path / "ws"
    monkeypatch.setenv("AUTODUB_WORKSPACE", str(ws))
    store = CloudJobStore(db_path=ws / "cloud_jobs.db", rollout_controlled=False)
    store.set_cu_budget_daily(0.2)

    dl_mock = Mock(side_effect=AssertionError("must not download when daily CU budget exceeded"))
    svc = CloudTelegramBotService(
        workspace_dir=ws,
        store=store,
        downloader_fn=dl_mock,
        adapter_factory=Mock,
    )
    gpu_mock = Mock(side_effect=AssertionError("must not run GPU when daily CU budget exceeded"))
    monkeypatch.setattr(svc.worker, "process_claimed_job", gpu_mock)

    async def exercise():
        svc._queue = asyncio.Queue()
        upd, _, edits = _make_update(user_id=777, text="https://youtu.be/dQw4w9WgXcQ")
        await svc.handle_text_message(upd, SimpleNamespace())
        item = await svc._queue.get()
        await svc._process_item(SimpleNamespace(), item)
        assert any("Tạm chặn để bảo vệ CU" in ed for ed in edits)
        dl_mock.assert_not_called()
        gpu_mock.assert_not_called()

    asyncio.run(exercise())


def test_oversized_telegram_video_over_20mb_rejected_gracefully(monkeypatch, tmp_path):
    monkeypatch.delenv("CLOUD_BOT_ALLOWED_USER_IDS", raising=False)
    ws = tmp_path / "ws"
    svc = CloudTelegramBotService(
        workspace_dir=ws,
        store=CloudJobStore(db_path=ws / "cloud_jobs.db", rollout_controlled=False),
        adapter_factory=Mock,
    )
    big_video = SimpleNamespace(
        file_id="big123",
        file_name="huge.mp4",
        file_size=TELEGRAM_BOT_DOWNLOAD_LIMIT_BYTES + 1024,
    )

    async def exercise():
        svc._queue = asyncio.Queue()
        upd, replies, _ = _make_update(user_id=777, video=big_video)
        await svc.handle_video_message(upd, SimpleNamespace())
        assert svc._queue.qsize() == 0
        assert any("20MB" in text for text, _ in replies)

    asyncio.run(exercise())


def test_extract_video_urls_strips_trailing_punctuation():
    urls = extract_video_urls("Link 1: https://www.youtube.com/watch?v=abc123, link 2: (https://tiktok.com/@a/video/1).")
    assert urls == ["https://www.youtube.com/watch?v=abc123", "https://tiktok.com/@a/video/1"]


def test_lifespan_starts_and_stops_cloud_telegram_bot_when_enabled(monkeypatch):
    from fastapi import FastAPI

    fake_bot = Mock()
    fake_bot.start_if_configured.return_value = True
    fake_bot.stop.return_value = True
    monkeypatch.setattr(node, "resolve_local_control_token", lambda: "offline-token")
    monkeypatch.setattr(node, "get_cloud_telegram_bot", lambda: fake_bot)
    monkeypatch.setattr(node, "peek_cloud_worker", lambda: None)

    async def exercise():
        app = FastAPI()
        app.state.autostart_worker = False
        app.state.owns_process_worker = True
        app.state.autostart_telegram_bot = True
        async with node.lifespan(app):
            fake_bot.start_if_configured.assert_called_once()
        fake_bot.stop.assert_called_once()
        assert app.state.node_shutdown_complete is True

    asyncio.run(exercise())


def test_cloud_status_endpoint_exposes_telegram_bot_metadata(monkeypatch, tmp_path):
    from fastapi.testclient import TestClient
    from backend.cloud import cloud_routes

    ws = tmp_path / "ws"
    (ws / "cloud_desktop").mkdir(parents=True)
    monkeypatch.setenv("AUTODUB_WORKSPACE", str(ws))
    monkeypatch.setenv("AUTODUB_LOCAL_TOKEN", "offline-test-token-1234567890")
    monkeypatch.setattr(cloud_routes, "get_usage", lambda: {"ok": True, "balance": 50.0, "usage_rate": 0.0})
    monkeypatch.setattr(cloud_routes, "get_sessions", lambda: [])

    client = TestClient(node.app)
    resp = client.get("/api/cloud/status", headers={"Host": "127.0.0.1:8089"})
    assert resp.status_code == 200
    body = resp.json()
    assert "telegram_bot" in body
    assert body["telegram_bot"]["configured"] is False

    (ws / "cloud_desktop" / ".cloud_bot_token").write_text(SYNTHETIC_BOT_TOKEN, encoding="utf-8")
    resp2 = client.get("/api/cloud/status", headers={"Host": "127.0.0.1:8089"})
    assert resp2.status_code == 200
    assert resp2.json()["telegram_bot"]["configured"] is True
    assert SYNTHETIC_BOT_TOKEN not in resp2.text


def test_orphan_session_reconciled_only_when_lease_expired(monkeypatch, tmp_path):
    ws = tmp_path / "ws"
    monkeypatch.setenv("AUTODUB_WORKSPACE", str(ws))
    store = CloudJobStore(db_path=ws / "cloud_jobs.db", rollout_controlled=False)

    fake_adapter = Mock()
    fake_adapter.get_sessions_status.return_value = {
        "ok": True,
        "sessions": [{"name": "cjoborphana1", "is_active": True}],
        "error": None,
    }
    fake_adapter.verify_session_stopped.return_value = {
        "verified_stopped": True,
        "cleanup_status": "verified",
        "attempts": 1,
        "error": None,
    }
    factory_mock = Mock(return_value=fake_adapter)

    svc = CloudTelegramBotService(
        workspace_dir=ws,
        store=store,
        adapter_factory=factory_mock,
    )
    factory_mock.reset_mock()

    # 1. Clean DB -> 0 provider calls
    assert svc._reconcile_orphans_sync() == []
    factory_mock.assert_not_called()

    # 2. Healthy running job (valid lease) -> 0 provider calls, VM untouched
    src_video = ws / "clip.mp4"
    src_video.parent.mkdir(parents=True, exist_ok=True)
    src_video.write_bytes(MP4_HEADER)
    job = store.enqueue(str(src_video), gpu_type="T4", voice="auto")
    claimed = store.claim_job(job.job_id, owner_id="dead-worker", lease_seconds=60.0)
    assert claimed is not None
    assert store.register_session(job.job_id, claimed.attempt, "dead-worker", "cjoborphana1")
    assert svc._reconcile_orphans_sync() == []
    factory_mock.assert_not_called()

    # 3. Expired lease -> orphaned VM stopped & verified, pending cleanup cleared
    with store._connect() as db:
        db.execute("UPDATE cloud_jobs SET lease_expires_at = 1.0 WHERE job_id = ?", (job.job_id,))
        db.commit()

    recovered = svc._reconcile_orphans_sync()
    assert len(recovered) == 1
    assert recovered[0]["job_id"] == job.job_id
    assert recovered[0]["cleanup_status"] == "verified"
    fake_adapter.verify_session_stopped.assert_called_once_with("cjoborphana1")
    assert store.list_pending_cleanup_events() == []
    assert store.get_active_job() is None


def test_stale_provider_snapshot_auto_refreshed_on_telegram_submission(monkeypatch, tmp_path):
    import time

    monkeypatch.delenv("CLOUD_BOT_ALLOWED_USER_IDS", raising=False)
    ws = tmp_path / "ws"
    cloud_out = tmp_path / "ToolV1_Cloud"
    cloud_out.mkdir(parents=True)
    monkeypatch.setenv("AUTODUB_WORKSPACE", str(ws))
    monkeypatch.setenv("AUTODUB_CLOUD_OUTPUT_DIR", str(cloud_out))

    store = CloudJobStore(db_path=ws / "cloud_jobs.db", rollout_controlled=False)
    store.record_provider_usage_snapshot(
        {"ok": True, "balance": 394.01, "observed_at": time.time() - 18765.0}
    )
    assert store.get_provider_usage_snapshot()["is_stale"] is True

    fake_adapter = Mock()
    fake_adapter.get_usage.return_value = {"ok": True, "balance": 393.82, "usage_rate": 0.0, "active_assignments": 0}

    def fake_downloader(url: str, dest_dir: str, tag: str):
        p = Path(dest_dir) / f"rednote_{tag}.mp4"
        p.write_bytes(MP4_HEADER)
        return True, str(p), "Rednote Clip", ""

    svc = CloudTelegramBotService(
        workspace_dir=ws,
        store=store,
        downloader_fn=fake_downloader,
        adapter_factory=lambda: fake_adapter,
    )

    def fake_process_claimed(job, progress_cb=None):
        out_video = cloud_out / f"cloud_result_{job.job_id}.mp4"
        out_video.write_bytes(MP4_HEADER)
        for st in ("uploading", "processing", "downloading", "validating"):
            assert store.update_progress(
                job.job_id, state=st, percent=60, step_name=st,
                owner_id=svc.worker.worker_id, attempt=job.attempt,
            )
        assert store.update_progress(
            job.job_id, state="completed", percent=100, step_name="Hoàn thành!",
            result_path=str(out_video), cu_actual=0.05,
            owner_id=svc.worker.worker_id, attempt=job.attempt, cleanup_status="verified",
        )
        return True

    monkeypatch.setattr(svc.worker, "process_claimed_job", fake_process_claimed)
    fake_bot = SimpleNamespace(send_message=AsyncMock())

    async def exercise():
        svc._queue = asyncio.Queue()
        upd, _, edits = _make_update(user_id=777, text="http://xhslink.com/o/abc123")
        await svc.handle_text_message(upd, SimpleNamespace())
        item = await svc._queue.get()
        await svc._process_item(fake_bot, item)
        assert not any("Số dư Colab đã cũ" in ed for ed in edits)
        fake_bot.send_message.assert_awaited_once()
        assert "Hoàn tất Cloud GPU" in fake_bot.send_message.await_args.kwargs["text"]
        fake_adapter.get_usage.assert_called()
        assert store.get_provider_usage_snapshot()["is_stale"] is False

    asyncio.run(exercise())


@pytest.mark.parametrize("update_from_store", [True, False])
def test_waiting_telegram_claim_uses_latest_usage(monkeypatch, tmp_path, update_from_store):
    ws = tmp_path / "ws"
    monkeypatch.setenv("AUTODUB_WORKSPACE", str(ws))
    monkeypatch.setenv("AUTODUB_CLOUD_OUTPUT_DIR", str(tmp_path / "cloud_output"))
    monkeypatch.setitem(cloud_config.DEFAULT_CONFIG, "provider_balance_stale_seconds", 300)
    clock = Mock(wraps=time)
    clock.time.return_value = time.time()
    monkeypatch.setattr(cloud_job_store, "time", clock)
    monkeypatch.setattr(cloud_telegram_bot, "time", clock)

    store = CloudJobStore(db_path=ws / "cloud_jobs.db", rollout_controlled=False)
    store.set_concurrency_limit(1)
    dashboard_video = ws / "dashboard.mp4"
    dashboard_video.write_bytes(MP4_HEADER)
    dashboard = store.enqueue(str(dashboard_video), options={"source_channel": "dashboard"})
    assert store.claim_job(dashboard.job_id, "dashboard-worker", lease_seconds=900) is not None
    store.record_provider_usage_snapshot({"ok": True, "balance": 100.0, "observed_at": clock.time() - 301})

    adapter = Mock()
    adapter.get_usage.side_effect = [{"ok": True, "balance": 50.0}, {"ok": True, "balance": 0.0}]

    def download(_url, dest_dir, _tag):
        video = Path(dest_dir) / "sample.mp4"
        video.write_bytes(MP4_HEADER)
        return True, str(video), "Test clip", ""

    svc = CloudTelegramBotService(workspace_dir=ws, store=store, downloader_fn=download, adapter_factory=lambda: adapter)
    process = Mock()
    monkeypatch.setattr(svc.worker, "process_claimed_job", process)

    async def exercise():
        waiting = asyncio.Event()
        edits = []

        async def edit_text(text, **_kwargs):
            edits.append(text)
            if "luồng Cloud trống" in text:
                waiting.set()

        item = CloudBotQueuedItem(
            item_id="usage-wait",
            kind="url",
            chat_id=777,
            source_label="test video",
            url="https://example.com/video.mp4",
            status_message=SimpleNamespace(edit_text=edit_text),
        )
        processing = asyncio.create_task(svc._process_item(SimpleNamespace(), item))
        await asyncio.wait_for(waiting.wait(), timeout=3)
        assert store.get_job(item.cloud_job_id).state == "queued"
        assert store.get_provider_usage_snapshot()["balance"] == 50.0
        if update_from_store:
            clock.time.return_value += 1
            store.record_provider_usage_snapshot({"ok": True, "balance": 0.0, "observed_at": clock.time()})
        else:
            clock.time.return_value += 301
            assert store.get_provider_usage_snapshot()["is_stale"]
        assert store.update_progress(
            dashboard.job_id,
            state="failed",
            owner_id="dashboard-worker",
            attempt=1,
            cleanup_status="verified",
        )
        await asyncio.wait_for(processing, timeout=3)

        assert any("Số dư tài khoản Colab không đủ" in text for text in edits)
        assert store.get_job(item.cloud_job_id).state == "cancelled"
        current = store.get_provider_usage_snapshot()
        assert (current["balance"], current["observed_at"], current["is_stale"]) == (0.0, clock.time(), False)

    asyncio.run(exercise())
    assert adapter.get_usage.call_count == (1 if update_from_store else 2)
    adapter.create_session.assert_not_called()
    process.assert_not_called()


def test_worker_get_current_job_reflects_live_sqlite_progress_and_lease(tmp_path):
    from backend.cloud.cloud_contract import get_cloud_status_snapshot
    from backend.cloud.cloud_worker import CloudWorker

    store = CloudJobStore(db_path=tmp_path / "jobs.db", rollout_controlled=False)
    vid = tmp_path / "v.mp4"
    vid.write_bytes(MP4_HEADER)
    enqueued = store.enqueue(str(vid), gpu_type="T4", voice="auto")
    claimed = store.claim_job(enqueued.job_id, owner_id="w-live", lease_seconds=5.0)
    assert claimed is not None and claimed.percent == 5

    worker = CloudWorker(worker_id="w-live", store=store)
    worker._current_job = claimed  # initial 5% snapshot at claim time

    assert store.register_session(claimed.job_id, claimed.attempt, "w-live", "cjoblivea1")
    assert store.update_progress(
        claimed.job_id,
        state="uploading",
        percent=20,
        step_name="Đang tải video...",
        owner_id="w-live",
        attempt=claimed.attempt,
    )
    assert store.update_progress(
        claimed.job_id,
        state="processing",
        percent=55,
        step_name="Đang chạy lồng tiếng...",
        owner_id="w-live",
        attempt=claimed.attempt,
    )
    assert store.heartbeat(claimed.job_id, owner_id="w-live", attempt=claimed.attempt, lease_seconds=120.0)

    live_cur = worker.get_current_job()
    assert live_cur is not None
    assert live_cur.percent == 55
    assert live_cur.state == "processing"
    assert live_cur.lease_expires_at > claimed.lease_expires_at + 60.0

    snap = get_cloud_status_snapshot(
        store,
        worker=worker,
        usage_fn=lambda: {"ok": True, "balance": 100.0, "usage_rate": 1.15, "active_assignments": 1},
        sessions_fn=lambda: [{"name": "cjoblivea1", "is_active": True}],
    )
    assert snap["active_job"]["percent"] == 55
    assert snap["active_job"]["state"] == "processing"



