"""H3: the platform slot must stay held while a job waits at the login/captcha gate.

Before the fix, _crawl_platform released the slot when it deferred, so a second
job for the same platform could start driving the same browser profile while
the first job's login page was still open.
"""
import asyncio
from unittest.mock import patch

import pytest

from backend.core.config import settings
from backend.models import SearchJob, SearchPlan
from backend.providers.base import SearchProvider, UserActionRequired
from backend.schemas.contracts import SearchFilters, VideoResult
from backend.services.search_service import SearchService


class GatedProvider(SearchProvider):
    """First 'q1' search raises a gate; the resume and other queries succeed."""

    capture_origin = "untrusted"

    def __init__(self):
        self.calls: list[str] = []
        self.gate = UserActionRequired(state="waiting_for_user", message="Cần giải CAPTCHA", partial=[])

    async def search(self, query: str, limit: int, filters: SearchFilters, on_batch=None) -> list[VideoResult]:
        self.calls.append(query)
        if query == "q1" and self.calls.count("q1") == 1:
            raise self.gate
        if query == "q2":
            return [VideoResult(
                platform="douyin", platform_video_id="dy_slot_002",
                title="Kết quả lượt hai", url="https://www.douyin.com/video/dy_slot_002",
            )]
        if query == "q3":
            return [VideoResult(
                platform="xiaohongshu", platform_video_id="xhs_slot_003",
                title="Kết quả nền tảng khác", url="https://www.xiaohongshu.com/explore/xhs_slot_003",
            )]
        return [VideoResult(
            platform="douyin", platform_video_id="dy_slot_001",
            title="Kết quả lượt một", url="https://www.douyin.com/video/dy_slot_001",
        )]

    async def close(self) -> None:
        pass


def _make_job(sessions, job_id: str, query: str, platform: str) -> None:
    with sessions() as db:
        db.add(SearchJob(
            id=job_id,
            original_query=query,
            platforms=[platform],
            requested_limit=1,
            is_mock=False,
        ))
        db.add(SearchPlan(job_id=job_id, queries=[query], use_ai=False))
        db.commit()


async def _wait_until_waiting(sessions, service: SearchService, job_id: str) -> None:
    for _ in range(150):
        await asyncio.sleep(0.02)
        with sessions() as db:
            job = db.get(SearchJob, job_id)
            if job and job.status in {"waiting_for_user", "waiting_for_login"}:
                return
    raise AssertionError(f"job {job_id} never reached the user-wait gate")


@pytest.mark.anyio
async def test_second_job_fails_fast_while_first_waits_at_the_gate(sessions, monkeypatch):
    monkeypatch.setattr(settings, "search_timeout", 0.5)
    _make_job(sessions, "slot-job-1", "q1", "douyin")
    _make_job(sessions, "slot-job-2", "q2", "douyin")
    provider = GatedProvider()
    service = SearchService(sessions=sessions, factory=lambda platform, _mock: provider)

    task1 = asyncio.create_task(service.pipeline("slot-job-1"))
    try:
        await _wait_until_waiting(sessions, service, "slot-job-1")
        # The waiting job must keep the platform slot: this is the H3 invariant.
        assert service.get_platform_lock("douyin").locked() is True

        await asyncio.wait_for(service.run("slot-job-2"), timeout=5)
        with sessions() as db:
            job2 = db.get(SearchJob, "slot-job-2")
            assert job2.status == "failed"
            assert "đang bận" in (job2.error_message or "")
        assert provider.calls == ["q1"], "job 2 must not touch the browser while job 1 waits"

        # Resolve the gate: job 1 finishes and releases the slot.
        service.waiters["slot-job-1"].set()
        await asyncio.wait_for(task1, timeout=5)
        with sessions() as db:
            job1 = db.get(SearchJob, "slot-job-1")
            assert job1.status == "completed"
            assert job1.provider_counts == {"douyin": 1}
        assert provider.calls == ["q1", "q1"]
        assert service.get_platform_lock("douyin").locked() is False

        # Now job 2 can run.
        await asyncio.wait_for(service.run("slot-job-2"), timeout=5)
        with sessions() as db:
            job2 = db.get(SearchJob, "slot-job-2")
            assert job2.status == "completed"
            assert job2.provider_counts == {"douyin": 1}
        assert provider.calls == ["q1", "q1", "q2"]
    finally:
        if not task1.done():
            task1.cancel()
            await asyncio.gather(task1, return_exceptions=True)


@pytest.mark.anyio
async def test_gate_timeout_releases_the_slot_for_the_next_job(sessions, monkeypatch):
    monkeypatch.setattr(settings, "search_timeout", 0.5)
    _make_job(sessions, "slot-timeout-1", "q1", "douyin")
    _make_job(sessions, "slot-timeout-2", "q2", "douyin")
    provider = GatedProvider()
    service = SearchService(sessions=sessions, factory=lambda platform, _mock: provider)

    orig_wait = service.wait_for_user

    async def fast_wait(job_id, gate, provider=None, platform="", timeout_seconds=None):
        return await orig_wait(job_id, gate, provider=provider, platform=platform, timeout_seconds=0.15)

    with patch.object(service, "wait_for_user", side_effect=fast_wait):
        await asyncio.wait_for(service.run("slot-timeout-1"), timeout=5)

    with sessions() as db:
        job1 = db.get(SearchJob, "slot-timeout-1")
        assert job1.status == "failed"
        assert "Đã hết thời gian chờ" in (job1.error_message or "")
    assert service.get_platform_lock("douyin").locked() is False

    await asyncio.wait_for(service.run("slot-timeout-2"), timeout=5)
    with sessions() as db:
        job2 = db.get(SearchJob, "slot-timeout-2")
        assert job2.status == "completed"


@pytest.mark.anyio
async def test_other_platform_is_not_blocked_by_a_waiting_gate(sessions, monkeypatch):
    monkeypatch.setattr(settings, "search_timeout", 0.5)
    _make_job(sessions, "slot-cross-1", "q1", "douyin")
    _make_job(sessions, "slot-cross-2", "q3", "xiaohongshu")
    provider = GatedProvider()
    service = SearchService(sessions=sessions, factory=lambda platform, _mock: provider)

    task1 = asyncio.create_task(service.pipeline("slot-cross-1"))
    try:
        await _wait_until_waiting(sessions, service, "slot-cross-1")
        await asyncio.wait_for(service.run("slot-cross-2"), timeout=5)
        with sessions() as db:
            job2 = db.get(SearchJob, "slot-cross-2")
            assert job2.status == "completed"
            assert job2.provider_counts == {"xiaohongshu": 1}
        service.waiters["slot-cross-1"].set()
        await asyncio.wait_for(task1, timeout=5)
        with sessions() as db:
            job1 = db.get(SearchJob, "slot-cross-1")
            assert job1.status == "completed"
    finally:
        if not task1.done():
            task1.cancel()
            await asyncio.gather(task1, return_exceptions=True)