import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from backend.models.search_job import SearchJob
from backend.providers.base import SearchProvider, UserActionRequired
from backend.services.search_service import SearchService


@pytest.mark.anyio
async def test_auto_resume_waiting_for_login_rejects_guest_page():
    """Auto-resume must NOT trigger when browser state is merely page_available (guest)."""
    service = SearchService(sessions=MagicMock())
    job_id = "test-job-login-guest"

    # Mock browser and page
    page = AsyncMock()
    page.is_closed = MagicMock(return_value=False)
    page.url = "https://www.xiaohongshu.com/explore"

    browser = MagicMock()
    browser.get_active_page = MagicMock(return_value=page)
    browser.page = page
    browser.diagnostics = {"is_authenticated": False}
    # Guest page returns page_available
    browser.inspect = AsyncMock(return_value="page_available")

    provider = MagicMock(spec=SearchProvider)
    provider.browser = browser

    gate = UserActionRequired(
        state="waiting_for_login",
        message="Cần đăng nhập tài khoản",
        partial=[],
    )

    with patch.object(service, "update") as mock_update:
        with pytest.raises(TimeoutError):
            await service.wait_for_user(
                job_id=job_id,
                gate=gate,
                provider=provider,
                platform="xiaohongshu",
                timeout_seconds=0.3,
            )

        # Event should not have set resuming
        assert not any(call.kwargs.get("status") == "resuming" for call in mock_update.call_args_list)


@pytest.mark.anyio
async def test_auto_resume_waiting_for_login_accepts_authenticated():
    """Auto-resume MUST trigger when browser inspect returns authenticated."""
    service = SearchService(sessions=MagicMock())
    job_id = "test-job-login-auth"

    page = AsyncMock()
    page.is_closed = MagicMock(return_value=False)
    page.url = "https://www.xiaohongshu.com/explore"

    browser = MagicMock()
    browser.get_active_page = MagicMock(return_value=page)
    browser.page = page
    browser.diagnostics = {"is_authenticated": True}
    browser.inspect = AsyncMock(return_value="authenticated")

    provider = MagicMock(spec=SearchProvider)
    provider.browser = browser

    gate = UserActionRequired(
        state="waiting_for_login",
        message="Cần đăng nhập tài khoản",
        partial=[],
    )

    with patch.object(service, "update") as mock_update:
        await service.wait_for_user(
            job_id=job_id,
            gate=gate,
            provider=provider,
            platform="xiaohongshu",
            timeout_seconds=2.0,
        )

        assert any(call.kwargs.get("status") == "resuming" for call in mock_update.call_args_list)


@pytest.mark.anyio
async def test_auto_resume_captcha_waits_until_container_cleared():
    """Auto-resume for CAPTCHA waits until locator is not visible."""
    service = SearchService(sessions=MagicMock())
    job_id = "test-job-captcha"

    page = AsyncMock()
    page.is_closed = MagicMock(return_value=False)
    page.url = "https://www.douyin.com"

    # First inspect: captcha visible, second: not visible
    locator_mock = AsyncMock()
    locator_mock.count = AsyncMock(side_effect=[1, 0])
    page.locator = MagicMock(return_value=locator_mock)

    browser = MagicMock()
    browser.get_active_page = MagicMock(return_value=page)
    browser.page = page
    browser.inspect = AsyncMock(return_value="page_available")

    provider = MagicMock(spec=SearchProvider)
    provider.browser = browser

    gate = UserActionRequired(
        state="waiting_for_user",
        message="Cần giải mã CAPTCHA",
        partial=[],
    )

    with patch.object(service, "update") as mock_update:
        await service.wait_for_user(
            job_id=job_id,
            gate=gate,
            provider=provider,
            platform="douyin",
            timeout_seconds=2.0,
        )

        assert any(call.kwargs.get("status") == "resuming" for call in mock_update.call_args_list)


@pytest.mark.anyio
async def test_verify_and_resume_returns_409_when_not_ready():
    """Manual resume returns False and explanatory Vietnamese reason when user is still unauthenticated."""
    service = SearchService(sessions=MagicMock())
    job_id = "test-job-verify-409"

    page = AsyncMock()
    page.is_closed = MagicMock(return_value=False)

    browser = MagicMock()
    browser.get_active_page = MagicMock(return_value=page)
    browser.page = page
    browser.diagnostics = {"is_authenticated": False}
    browser.inspect = AsyncMock(return_value="login_required")

    provider = MagicMock(spec=SearchProvider)
    provider.browser = browser

    gate = UserActionRequired(
        state="waiting_for_login",
        message="Cần đăng nhập",
        partial=[],
    )

    # Register in waiters and waiting_contexts
    event = asyncio.Event()
    service.waiters[job_id] = event
    service.waiting_contexts[job_id] = (provider, gate, "xiaohongshu")

    can_resume, reason = await service.verify_and_resume(job_id)
    assert not can_resume
    assert "chưa hoàn tất đăng nhập" in reason
    assert not event.is_set()

    # Now simulate successful authentication
    browser.diagnostics = {"is_authenticated": True}
    browser.inspect = AsyncMock(return_value="authenticated")

    can_resume_ok, reason_ok = await service.verify_and_resume(job_id)
    assert can_resume_ok
    assert event.is_set()
    assert "thành công" in reason_ok


@pytest.mark.anyio
async def test_cancel_waiting_job_releases_cleanly():
    """Canceling a waiting job releases the event waiter and updates status to cancelled."""
    mock_db = MagicMock()
    mock_job = MagicMock()
    mock_job.status = "waiting_for_user"
    mock_job.processed_count = 0
    mock_db.get.return_value = mock_job
    mock_sessions = MagicMock()
    mock_sessions.return_value.__enter__.return_value = mock_db

    service = SearchService(sessions=mock_sessions)
    job_id = "test-job-cancel"

    event = asyncio.Event()
    service.waiters[job_id] = event

    await service.cancel(job_id)
    assert event.is_set()
    assert mock_job.status == "cancelled"


@pytest.mark.anyio
async def test_multi_platform_ordering_douyin_then_xiaohongshu():
    """Verify active_platform is tracked accurately when searching Douyin then Xiaohongshu."""
    mock_db = MagicMock()
    mock_job = MagicMock()
    mock_job.status = "pending"
    mock_job.diagnostics = {}
    mock_job.processed_count = 0
    mock_db.get.return_value = mock_job
    mock_sessions = MagicMock()
    mock_sessions.return_value.__enter__.return_value = mock_db

    service = SearchService(sessions=mock_sessions)
    job_id = "test-job-dy-xhs"

    active_platforms_seen = []

    def mock_update(jid, **kwargs):
        if "active_platform" in kwargs:
            active_platforms_seen.append(kwargs["active_platform"])

    service.update = MagicMock(side_effect=mock_update)

    # Simulate platform iteration tracking
    for platform in ["douyin", "xiaohongshu"]:
        service.update(job_id, active_platform=platform)

    assert active_platforms_seen == ["douyin", "xiaohongshu"]


@pytest.mark.anyio
async def test_multi_platform_ordering_xiaohongshu_then_douyin():
    """Verify active_platform is tracked accurately when searching Xiaohongshu then Douyin."""
    mock_db = MagicMock()
    mock_job = MagicMock()
    mock_job.status = "pending"
    mock_job.diagnostics = {}
    mock_job.processed_count = 0
    mock_db.get.return_value = mock_job
    mock_sessions = MagicMock()
    mock_sessions.return_value.__enter__.return_value = mock_db

    service = SearchService(sessions=mock_sessions)
    job_id = "test-job-xhs-dy"

    active_platforms_seen = []

    def mock_update(jid, **kwargs):
        if "active_platform" in kwargs:
            active_platforms_seen.append(kwargs["active_platform"])

    service.update = MagicMock(side_effect=mock_update)

    for platform in ["xiaohongshu", "douyin"]:
        service.update(job_id, active_platform=platform)

    assert active_platforms_seen == ["xiaohongshu", "douyin"]


def test_serialize_job_includes_active_platform_and_waiting_reason():
    """Job serialization includes active_platform and waiting_reason with fallback to diagnostics."""
    from datetime import datetime, timezone

    from backend.api.search import serialize_job

    now = datetime.now(timezone.utc)
    job = SearchJob(
        id="test-job-serial",
        original_query="decor",
        status="waiting_for_login",
        platforms=["douyin", "xiaohongshu"],
        active_platform="xiaohongshu",
        waiting_reason="Chờ đăng nhập tài khoản Xiaohongshu",
        requested_limit=10,
        found_count=0,
        processed_count=0,
        duplicate_count=0,
        rejected_count=0,
        provider_counts={},
        is_mock=False,
        ai_fallback_occurred=False,
        diagnostics={},
        created_at=now,
    )
    db = MagicMock()
    db.get.return_value = None

    job_out = serialize_job(job, db)
    assert job_out.active_platform == "xiaohongshu"
    assert job_out.waiting_reason == "Chờ đăng nhập tài khoản Xiaohongshu"

    # Also test fallback from diagnostics if model columns are None
    job2 = SearchJob(
        id="test-job-diag-fallback",
        original_query="decor",
        status="waiting_for_user",
        platforms=["douyin"],
        active_platform=None,
        waiting_reason=None,
        requested_limit=10,
        found_count=0,
        processed_count=0,
        duplicate_count=0,
        rejected_count=0,
        provider_counts={},
        is_mock=False,
        ai_fallback_occurred=False,
        diagnostics={"active_platform": "douyin", "waiting_reason": "Cần giải CAPTCHA"},
        created_at=now,
    )
    job_out2 = serialize_job(job2, db)
    assert job_out2.active_platform == "douyin"
    assert job_out2.waiting_reason == "Cần giải CAPTCHA"

