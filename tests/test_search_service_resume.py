"""Pipeline Orchestration Integration Tests.

NOTE: These tests evaluate SearchService.pipeline() coordination, real SQLite persistence,
active_platform tracking, deduplication across partial retries, and manual action resume
using controlled mock providers and real SQLite database sessions.
They verify service-layer orchestration logic, NOT live crawler network acceptance
(live crawler acceptance tests reside in tests/test_real_crawl_acceptance.py).
"""

import asyncio
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from backend.api.search import serialize_job
from backend.models import JobVideo, SearchJob, SearchPlan, Video
from backend.providers.base import (
    ManualActionTimeoutError,
    SearchProvider,
    UserActionRequired,
)
from backend.schemas.contracts import SearchFilters, VideoResult
from backend.services.search_service import SearchService


class FakeMultiPlatformProvider(SearchProvider):
    def __init__(self, platform: str, results_or_gate, browser=None):
        self.platform = platform
        self.results_or_gate = results_or_gate
        self.browser = browser
        self.call_count = 0

    async def search(self, query: str, limit: int, filters: SearchFilters, on_batch=None) -> list[VideoResult]:
        self.call_count += 1
        item = self.results_or_gate
        if callable(item):
            item = item(self.call_count)
        if isinstance(item, Exception):
            raise item
        return item

    async def close(self) -> None:
        pass


@pytest.mark.anyio
async def test_auto_resume_waiting_for_login_rejects_guest_page():
    """Auto-resume must NOT trigger when browser state is merely page_available (guest)."""
    service = SearchService(sessions=MagicMock())
    job_id = "test-job-login-guest"

    page = AsyncMock()
    page.is_closed = MagicMock(return_value=False)
    page.url = "https://www.xiaohongshu.com/explore"

    browser = MagicMock()
    browser.get_active_page = MagicMock(return_value=page)
    browser.page = page
    browser.diagnostics = {"is_authenticated": False}
    browser.inspect = AsyncMock(return_value="page_available")

    provider = MagicMock(spec=SearchProvider)
    provider.browser = browser

    gate = UserActionRequired(
        state="waiting_for_login",
        message="Cần đăng nhập tài khoản",
        partial=[],
    )

    with patch.object(service, "update") as mock_update:
        with pytest.raises(ManualActionTimeoutError) as exc_info:
            await service.wait_for_user(
                job_id=job_id,
                gate=gate,
                provider=provider,
                platform="xiaohongshu",
                timeout_seconds=0.3,
            )

        assert "Đã hết thời gian chờ đăng nhập trên Xiaohongshu" in str(exc_info.value)
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
async def test_real_pipeline_multi_platform_douyin_then_xiaohongshu_blocked_on_second(sessions):
    """Real pipeline integration: Douyin completes, Xiaohongshu blocks on login.
    Job must save Douyin results, set active_platform='xiaohongshu', waiting_reason='login_required'.
    Resume fails with 409 if unready, and finishes cleanly upon authentication without duplication.
    """
    job_id = "test-pipe-dy-xhs"
    now = datetime.now(timezone.utc)
    with sessions() as db:
        job = SearchJob(
            id=job_id,
            original_query="decor bàn học vintage",
            status="pending",
            platforms=["douyin", "xiaohongshu"],
            requested_limit=4,
            is_mock=False,
            created_at=now,
        )
        plan = SearchPlan(
            job_id=job_id,
            queries=["复古书桌布置", "复古桌面改造"],
            use_ai=False,
        )
        db.add(job)
        db.add(plan)
        db.commit()

    dy_res = [
        VideoResult(
            platform="douyin",
            platform_video_id="dy_vid_001",
            title="Decor bàn học vintage cổ điển 复古书桌",
            url="https://www.douyin.com/video/dy_vid_001",
            author_name="DecorLover",
            like_count=12000,
        ),
        VideoResult(
            platform="douyin",
            platform_video_id="dy_vid_002",
            title="Góc học tập phong cách vintage 复古桌面",
            url="https://www.douyin.com/video/dy_vid_002",
            author_name="StudyVlog",
            like_count=8500,
        ),
    ]

    xhs_res = [
        VideoResult(
            platform="xiaohongshu",
            platform_video_id="xhs_vid_001",
            title="Review góc học tập vintage xinh xắn 复古书桌布置",
            url="https://www.xiaohongshu.com/explore/xhs_vid_001",
            author_name="XhsCreator",
            like_count=6400,
        ),
        VideoResult(
            platform="xiaohongshu",
            platform_video_id="xhs_vid_002",
            title="Gợi ý đồ decor bàn học vintage 复古桌面好物",
            url="https://www.xiaohongshu.com/explore/xhs_vid_002",
            author_name="VintageHome",
            like_count=4300,
        ),
    ]

    xhs_page = AsyncMock()
    xhs_page.is_closed = MagicMock(return_value=False)
    xhs_page.url = "https://www.xiaohongshu.com/explore"
    xhs_browser = MagicMock()
    xhs_browser.get_active_page = MagicMock(return_value=xhs_page)
    xhs_browser.page = xhs_page
    xhs_browser.diagnostics = {"is_authenticated": False}
    xhs_browser.inspect = AsyncMock(return_value="login_required")

    gate = UserActionRequired(
        state="waiting_for_login",
        message="Xiaohongshu yêu cầu đăng nhập",
        partial=[],
    )

    def xhs_behavior(call_count):
        if call_count == 1:
            raise gate
        return xhs_res

    dy_provider = FakeMultiPlatformProvider("douyin", dy_res)
    xhs_provider = FakeMultiPlatformProvider("xiaohongshu", xhs_behavior, browser=xhs_browser)

    def provider_factory(platform: str, use_mock: bool):
        if platform == "douyin":
            return dy_provider
        return xhs_provider

    service = SearchService(sessions=sessions, factory=provider_factory)

    pipeline_task = asyncio.create_task(service.pipeline(job_id))

    for _ in range(50):
        await asyncio.sleep(0.05)
        with sessions() as db:
            j = db.get(SearchJob, job_id)
            if j and j.status == "waiting_for_login":
                break

    with sessions() as db:
        j = db.get(SearchJob, job_id)
        assert j is not None
        assert j.status == "waiting_for_login"
        assert j.active_platform == "xiaohongshu"
        assert j.waiting_reason == "login_required"
        assert j.provider_counts.get("douyin") == 2
        job_vids = db.query(JobVideo).filter_by(job_id=job_id).all()
        assert len(job_vids) == 2
        v_ids = [jv.video_id for jv in job_vids]
        videos = db.query(Video).filter(Video.id.in_(v_ids)).all()
        assert len(videos) == 2
        assert all(v.platform == "douyin" for v in videos)

    # 1. Manual resume fails while unauthenticated
    can_resume, reason = await service.verify_and_resume(job_id)
    assert can_resume is False
    assert "chưa hoàn tất đăng nhập" in reason

    # 2. Authenticate and resume
    xhs_browser.diagnostics = {"is_authenticated": True}
    xhs_browser.inspect = AsyncMock(return_value="authenticated")

    can_resume_ok, reason_ok = await service.verify_and_resume(job_id)
    assert can_resume_ok is True
    assert "thành công" in reason_ok

    await asyncio.wait_for(pipeline_task, timeout=5.0)

    with sessions() as db:
        final_job = db.get(SearchJob, job_id)
        assert final_job is not None
        assert final_job.status == "completed"
        assert final_job.provider_counts.get("douyin") == 2
        assert final_job.provider_counts.get("xiaohongshu") == 2
        assert final_job.processed_count == 4

        all_job_vids = db.query(JobVideo).filter_by(job_id=job_id).all()
        assert len(all_job_vids) == 4
        all_vid_ids = [jv.video_id for jv in all_job_vids]
        assert len(set(all_vid_ids)) == 4


@pytest.mark.anyio
async def test_real_pipeline_multi_platform_xiaohongshu_then_douyin_blocked_on_second(sessions):
    """Real pipeline integration: Xiaohongshu completes, Douyin blocks on CAPTCHA.
    Job must save XHS results, set active_platform='douyin', waiting_reason='verification_required'.
    Resume fails while CAPTCHA is present, and finishes cleanly upon clearing without duplication.
    """
    job_id = "test-pipe-xhs-dy"
    now = datetime.now(timezone.utc)
    with sessions() as db:
        job = SearchJob(
            id=job_id,
            original_query="bàn phím cơ giá rẻ",
            status="pending",
            platforms=["xiaohongshu", "douyin"],
            requested_limit=4,
            is_mock=False,
            created_at=now,
        )
        plan = SearchPlan(
            job_id=job_id,
            queries=["百元平价机械键盘", "学生性价比机械键盘"],
            use_ai=False,
        )
        db.add(job)
        db.add(plan)
        db.commit()

    xhs_res = [
        VideoResult(
            platform="xiaohongshu",
            platform_video_id="xhs_kbd_001",
            title="Review bàn phím cơ giá rẻ sinh viên 平价机械键盘",
            url="https://www.xiaohongshu.com/explore/xhs_kbd_001",
            author_name="KbdReviewer",
            like_count=5200,
        ),
        VideoResult(
            platform="xiaohongshu",
            platform_video_id="xhs_kbd_002",
            title="Top 3 bàn phím cơ gõ êm giá sinh viên 性价比机械键盘",
            url="https://www.xiaohongshu.com/explore/xhs_kbd_002",
            author_name="TechDaily",
            like_count=4100,
        ),
    ]

    dy_res = [
        VideoResult(
            platform="douyin",
            platform_video_id="dy_kbd_001",
            title="Bàn phím cơ sinh viên gõ cực sướng 百元机械键盘",
            url="https://www.douyin.com/video/dy_kbd_001",
            author_name="DouyinTech",
            like_count=18000,
        ),
        VideoResult(
            platform="douyin",
            platform_video_id="dy_kbd_002",
            title="Đập hộp bàn phím cơ hot swap 机械键盘开箱",
            url="https://www.douyin.com/video/dy_kbd_002",
            author_name="UnboxTech",
            like_count=9300,
        ),
    ]

    dy_page = AsyncMock()
    dy_page.is_closed = MagicMock(return_value=False)
    dy_page.url = "https://www.douyin.com"
    dy_browser = MagicMock()
    dy_browser.get_active_page = MagicMock(return_value=dy_page)
    dy_browser.page = dy_page
    dy_browser.inspect = AsyncMock(return_value="verification_required")

    gate = UserActionRequired(
        state="waiting_for_user",
        message="Douyin yêu cầu giải mã xác minh",
        partial=[],
    )

    def dy_behavior(call_count):
        if call_count == 1:
            raise gate
        return dy_res

    xhs_provider = FakeMultiPlatformProvider("xiaohongshu", xhs_res)
    dy_provider = FakeMultiPlatformProvider("douyin", dy_behavior, browser=dy_browser)

    def provider_factory(platform: str, use_mock: bool):
        if platform == "xiaohongshu":
            return xhs_provider
        return dy_provider

    service = SearchService(sessions=sessions, factory=provider_factory)

    pipeline_task = asyncio.create_task(service.pipeline(job_id))

    for _ in range(50):
        await asyncio.sleep(0.05)
        with sessions() as db:
            j = db.get(SearchJob, job_id)
            if j and j.status == "waiting_for_user":
                break

    with sessions() as db:
        j = db.get(SearchJob, job_id)
        assert j is not None
        assert j.status == "waiting_for_user"
        assert j.active_platform == "douyin"
        assert j.waiting_reason == "verification_required"
        assert j.provider_counts.get("xiaohongshu") == 2
        job_vids = db.query(JobVideo).filter_by(job_id=job_id).all()
        assert len(job_vids) == 2
        v_ids = [jv.video_id for jv in job_vids]
        videos = db.query(Video).filter(Video.id.in_(v_ids)).all()
        assert len(videos) == 2
        assert all(v.platform == "xiaohongshu" for v in videos)

    # 1. Manual resume fails while CAPTCHA is active
    can_resume, reason = await service.verify_and_resume(job_id)
    assert can_resume is False
    assert "chưa được giải xong" in reason

    # 2. CAPTCHA solved, page becomes available
    dy_browser.inspect = AsyncMock(return_value="page_available")

    can_resume_ok, reason_ok = await service.verify_and_resume(job_id)
    assert can_resume_ok is True
    assert "thành công" in reason_ok

    await asyncio.wait_for(pipeline_task, timeout=5.0)

    with sessions() as db:
        final_job = db.get(SearchJob, job_id)
        assert final_job is not None
        assert final_job.status == "completed"
        assert final_job.provider_counts.get("xiaohongshu") == 2
        assert final_job.provider_counts.get("douyin") == 2
        assert final_job.processed_count == 4

        all_job_vids = db.query(JobVideo).filter_by(job_id=job_id).all()
        assert len(all_job_vids) == 4
        all_vid_ids = [jv.video_id for jv in all_job_vids]
        assert len(set(all_vid_ids)) == 4


@pytest.mark.anyio
async def test_real_pipeline_resumes_without_duplicate_results(sessions):
    """When a provider yields partial results then raises UserActionRequired,
    resuming and re-fetching the full batch does NOT create duplicate JobVideo rows."""
    job_id = "test-pipe-dedup"
    now = datetime.now(timezone.utc)
    with sessions() as db:
        job = SearchJob(
            id=job_id,
            original_query="nhíp gắp sticker",
            status="pending",
            platforms=["douyin"],
            requested_limit=2,
            is_mock=False,
            created_at=now,
        )
        plan = SearchPlan(
            job_id=job_id,
            queries=["贴纸镊子"],
            use_ai=False,
        )
        db.add(job)
        db.add(plan)
        db.commit()

    v1 = VideoResult(
        platform="douyin",
        platform_video_id="dy_dup_001",
        title="Nhíp gắp sticker chuyên dụng 贴纸镊子分享",
        url="https://www.douyin.com/video/dy_dup_001",
        author_name="StationeryFan",
        like_count=10000,
    )
    v2 = VideoResult(
        platform="douyin",
        platform_video_id="dy_dup_002",
        title="Top nhíp gắp sticker cong 咕卡手账镊子",
        url="https://www.douyin.com/video/dy_dup_002",
        author_name="StationeryFan2",
        like_count=8000,
    )

    page = AsyncMock()
    page.is_closed = MagicMock(return_value=False)
    browser = MagicMock()
    browser.get_active_page = MagicMock(return_value=page)
    browser.page = page
    browser.inspect = AsyncMock(return_value="verification_required")

    gate = UserActionRequired(
        state="waiting_for_user",
        message="Cần giải CAPTCHA",
        partial=[v1],
    )

    def dy_behavior(call_count):
        if call_count == 1:
            raise gate
        return [v1, v2]

    dy_provider = FakeMultiPlatformProvider("douyin", dy_behavior, browser=browser)
    service = SearchService(sessions=sessions, factory=lambda p, m: dy_provider)

    pipeline_task = asyncio.create_task(service.pipeline(job_id))

    for _ in range(50):
        await asyncio.sleep(0.05)
        with sessions() as db:
            j = db.get(SearchJob, job_id)
            if j and j.status == "waiting_for_user":
                break

    with sessions() as db:
        j = db.get(SearchJob, job_id)
        assert j is not None
        assert j.status == "waiting_for_user"
        # Partial v1 should already be stored
        assert j.processed_count == 1
        job_vids = db.query(JobVideo).filter_by(job_id=job_id).all()
        assert len(job_vids) == 1

    # Clear CAPTCHA and resume
    browser.inspect = AsyncMock(return_value="page_available")
    can_resume, _ = await service.verify_and_resume(job_id)
    assert can_resume is True

    await asyncio.wait_for(pipeline_task, timeout=5.0)

    with sessions() as db:
        final_job = db.get(SearchJob, job_id)
        assert final_job is not None
        assert final_job.status == "completed"
        # Exactly 2 videos processed, not 3
        assert final_job.processed_count == 2
        all_job_vids = db.query(JobVideo).filter_by(job_id=job_id).all()
        assert len(all_job_vids) == 2
        all_vid_ids = [jv.video_id for jv in all_job_vids]
        assert len(set(all_vid_ids)) == 2


@pytest.mark.anyio
async def test_pipeline_manual_action_timeout_sets_explicit_error_message(sessions):
    """When waiting for user manual action times out, the error message clearly indicates
    manual interaction timeout rather than general search execution timeout."""
    job_id = "test-pipe-timeout"
    now = datetime.now(timezone.utc)
    with sessions() as db:
        job = SearchJob(
            id=job_id,
            original_query="decor bàn học",
            status="pending",
            platforms=["xiaohongshu"],
            requested_limit=2,
            is_mock=False,
            created_at=now,
        )
        plan = SearchPlan(
            job_id=job_id,
            queries=["书桌布置"],
            use_ai=False,
        )
        db.add(job)
        db.add(plan)
        db.commit()

    page = AsyncMock()
    page.is_closed = MagicMock(return_value=False)
    browser = MagicMock()
    browser.get_active_page = MagicMock(return_value=page)
    browser.page = page
    browser.diagnostics = {"is_authenticated": False}
    browser.inspect = AsyncMock(return_value="login_required")

    gate = UserActionRequired(
        state="waiting_for_login",
        message="Cần đăng nhập",
        partial=[],
    )

    def xhs_behavior(call_count):
        raise gate

    provider = FakeMultiPlatformProvider("xiaohongshu", xhs_behavior, browser=browser)
    service = SearchService(sessions=sessions, factory=lambda p, m: provider)

    # Patch wait_for_user timeout to 0.15s to simulate fast expiration
    orig_wait = service.wait_for_user

    async def fast_wait_for_user(jid, g, provider=None, platform="", timeout_seconds=None):
        return await orig_wait(jid, g, provider=provider, platform=platform, timeout_seconds=0.15)

    with patch.object(service, "wait_for_user", side_effect=fast_wait_for_user):
        await service.run(job_id)

    with sessions() as db:
        final_job = db.get(SearchJob, job_id)
        assert final_job is not None
        assert final_job.status == "failed"
        assert "Đã hết thời gian chờ đăng nhập trên Xiaohongshu" in (final_job.error_message or "")
        # Must NOT report generic search timeout message
        assert "Tìm kiếm quá thời gian cho phép" not in (final_job.error_message or "")


def test_endpoint_manual_resume_returns_http_409(client, sessions):
    """Endpoint POST /api/search/jobs/{job_id}/resume returns HTTP 409 Conflict with Vietnamese
    explanation when browser is unready, and HTTP 200 OK when ready."""
    job_id = "test-endpoint-409"
    now = datetime.now(timezone.utc)
    with sessions() as db:
        job = SearchJob(
            id=job_id,
            original_query="decor bàn học",
            status="waiting_for_login",
            platforms=["xiaohongshu"],
            active_platform="xiaohongshu",
            waiting_reason="login_required",
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
        db.add(job)
        db.commit()

    page = AsyncMock()
    page.is_closed = MagicMock(return_value=False)
    browser = MagicMock()
    browser.get_active_page = MagicMock(return_value=page)
    browser.page = page
    browser.diagnostics = {"is_authenticated": False}
    browser.inspect = AsyncMock(return_value="login_required")

    provider = MagicMock(spec=SearchProvider)
    provider.browser = browser
    gate = UserActionRequired(state="waiting_for_login", message="Cần đăng nhập", partial=[])

    search_svc = client.app.state.search
    event = asyncio.Event()
    search_svc.waiters[job_id] = event
    search_svc.waiting_contexts[job_id] = (provider, gate, "xiaohongshu")

    # 1. Unready -> HTTP 409
    resp = client.post(f"/api/search/jobs/{job_id}/resume")
    assert resp.status_code == 409
    assert "chưa hoàn tất đăng nhập" in resp.json()["detail"]

    # 2. Authenticated -> HTTP 200
    browser.diagnostics = {"is_authenticated": True}
    browser.inspect = AsyncMock(return_value="authenticated")

    resp_ok = client.post(f"/api/search/jobs/{job_id}/resume")
    assert resp_ok.status_code == 200
    data = resp_ok.json()
    assert data["id"] == job_id


def test_serialize_job_includes_active_platform_and_waiting_reason():
    """Job serialization includes active_platform and waiting_reason with fallback to diagnostics."""
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
