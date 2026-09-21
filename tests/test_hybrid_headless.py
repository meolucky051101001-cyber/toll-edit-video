import asyncio
import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from backend.app.main import app
from backend.core.config import settings
from backend.core.database import Base, engine
from backend.models import JobVideo, SearchJob, Video
from backend.providers.base import UserActionRequired
from backend.providers.douyin.provider import DouyinProvider
from backend.providers.registry import get_provider
from backend.providers.xiaohongshu.provider import XiaohongshuProvider
from backend.schemas.contracts import VideoResult
from backend.services.browser_service import BrowserService
from backend.services.douyin_browser_service import DouyinBrowserService
from backend.services.download.browser_resolver import BrowserMediaResolver
from backend.services.search_service import SearchService


# 1. Config headless default
def test_config_headless_defaults():
    from backend.core.config import Settings

    raw = Settings(_env_file=None)
    assert raw.playwright_headless is True
    assert raw.hybrid_headless is True
    assert raw.browser_auto_elevate is True
    assert raw.browser_interaction_timeout == 1800

    assert settings.playwright_headless is True
    assert settings.hybrid_headless is True
    assert settings.browser_auto_elevate is True
    assert settings.browser_interaction_timeout == 1800


# 2. Hybrid disabled
def test_hybrid_disabled(tmp_path: Path):
    svc = BrowserService(profile=tmp_path, headless=False, hybrid_headless=False)
    assert svc.should_run_headless() is False

    auth_file = tmp_path / "auth_state.json"
    auth_file.write_text(
        json.dumps({
            "grade": "high_quality",
            "cookies": [{"name": "web_session", "value": "xyz", "domain": ".xiaohongshu.com"}],
        }),
        encoding="utf-8",
    )
    assert svc.should_run_headless() is False


# 3. Production-like service: headless=True meets elevation -> decides HEADFUL
@pytest.mark.anyio
async def test_production_service_headless_elevation(tmp_path: Path):
    # Production initializes with headless=True
    svc = BrowserService(profile=tmp_path, headless=True, hybrid_headless=True)
    assert svc.should_run_headless() is True

    svc.is_currently_headless = True
    mock_page = MagicMock()
    mock_page.url = "https://www.xiaohongshu.com/explore"
    mock_page.is_closed.return_value = False
    mock_page.goto = AsyncMock()
    svc.page = mock_page

    with (
        patch.object(svc, "_close", new_callable=AsyncMock) as mock_close,
        patch.object(svc, "ensure_open", new_callable=AsyncMock) as mock_ensure,
        patch.object(svc, "inspect", new_callable=AsyncMock),
    ):
        new_page = MagicMock()
        mock_ensure.return_value = new_page

        await svc.elevate_to_headful()

        # Crucial Phase 1 assertion: force_headful_session overrides headless=True
        assert svc.force_headful_session is True
        assert svc.hybrid_headless is False
        assert svc.should_run_headless() is False
        mock_close.assert_called_once()
        mock_ensure.assert_called_once_with(headless_override=False)

    # Repeat check for Douyin
    dy_svc = DouyinBrowserService(profile=tmp_path, headless=True, hybrid_headless=True)
    assert dy_svc.should_run_headless() is True
    dy_svc.is_currently_headless = True
    dy_svc.page = mock_page
    with (
        patch.object(dy_svc, "_close", new_callable=AsyncMock) as mock_close,
        patch.object(dy_svc, "ensure_open", new_callable=AsyncMock) as mock_ensure,
        patch.object(dy_svc, "inspect", new_callable=AsyncMock),
    ):
        mock_ensure.return_value = new_page
        await dy_svc.elevate_to_headful()
        assert dy_svc.force_headful_session is True
        assert dy_svc.should_run_headless() is False
        mock_ensure.assert_called_once_with(headless_override=False)


# 4. Provider auto_elevate=False
@pytest.mark.anyio
async def test_provider_auto_elevate_disabled():
    browser = MagicMock(spec=BrowserService)
    browser.is_currently_headless = True
    browser.inspect = AsyncMock(return_value="login_required")
    browser.elevate_to_headful = AsyncMock()

    provider = XiaohongshuProvider(browser=browser, auto_elevate=False)
    with pytest.raises(UserActionRequired):
        await provider.gate(MagicMock(), [])

    browser.elevate_to_headful.assert_not_called()


# 5. Provider auto_elevate=True
@pytest.mark.anyio
async def test_provider_auto_elevate_enabled():
    browser = MagicMock(spec=BrowserService)
    browser.is_currently_headless = True
    browser.inspect = AsyncMock(return_value="login_required")
    browser.elevate_to_headful = AsyncMock()

    provider = XiaohongshuProvider(browser=browser, auto_elevate=True)
    with pytest.raises(UserActionRequired):
        await provider.gate(MagicMock(), [])

    browser.elevate_to_headful.assert_called_once()


# 6. Registry propagates auto_elevate config
def test_registry_propagates_auto_elevate_config():
    browser = MagicMock(spec=BrowserService)
    dy_browser = MagicMock(spec=DouyinBrowserService)

    # Explicit False
    xhs_prov_false = get_provider("xiaohongshu", False, browser=browser, auto_elevate=False)
    assert getattr(xhs_prov_false, "auto_elevate") is False

    # Explicit True
    xhs_prov_true = get_provider("xiaohongshu", False, browser=browser, auto_elevate=True)
    assert getattr(xhs_prov_true, "auto_elevate") is True

    # Douyin False
    dy_prov_false = get_provider("douyin", False, douyin_browser=dy_browser, auto_elevate=False)
    assert getattr(dy_prov_false, "auto_elevate") is False

    # Default from settings (which is True)
    xhs_prov_default = get_provider("xiaohongshu", False, browser=browser)
    assert getattr(xhs_prov_default, "auto_elevate") == settings.browser_auto_elevate


# 7. XHS login_required elevation
@pytest.mark.anyio
async def test_xhs_login_required_elevation():
    browser = MagicMock(spec=BrowserService)
    browser.is_currently_headless = True
    browser.inspect = AsyncMock(return_value="login_required")
    browser.elevate_to_headful = AsyncMock()

    provider = XiaohongshuProvider(browser=browser, auto_elevate=True)
    with pytest.raises(UserActionRequired) as exc_info:
        await provider.gate(MagicMock(), [])

    assert exc_info.value.state == "waiting_for_login"
    browser.elevate_to_headful.assert_called_once()


# 8. XHS verification_required elevation
@pytest.mark.anyio
async def test_xhs_verification_required_elevation():
    browser = MagicMock(spec=BrowserService)
    browser.is_currently_headless = True
    browser.inspect = AsyncMock(return_value="verification_required")
    browser.elevate_to_headful = AsyncMock()

    provider = XiaohongshuProvider(browser=browser, auto_elevate=True)
    with pytest.raises(UserActionRequired) as exc_info:
        await provider.gate(MagicMock(), [])

    assert exc_info.value.state == "waiting_for_user"
    browser.elevate_to_headful.assert_called_once()


# 9. Douyin login_required elevation
@pytest.mark.anyio
async def test_douyin_login_required_elevation():
    browser = MagicMock(spec=DouyinBrowserService)
    browser.is_currently_headless = True
    browser.inspect = AsyncMock(return_value="login_required")
    browser.elevate_to_headful = AsyncMock()

    provider = DouyinProvider(browser=browser, auto_elevate=True)
    with pytest.raises(UserActionRequired) as exc_info:
        await provider.gate(MagicMock(), [])

    assert exc_info.value.state == "waiting_for_login"
    browser.elevate_to_headful.assert_called_once()


# 10. Douyin verification_required elevation
@pytest.mark.anyio
async def test_douyin_verification_required_elevation():
    browser = MagicMock(spec=DouyinBrowserService)
    browser.is_currently_headless = True
    browser.inspect = AsyncMock(return_value="verification_required")
    browser.elevate_to_headful = AsyncMock()

    provider = DouyinProvider(browser=browser, auto_elevate=True)
    with pytest.raises(UserActionRequired) as exc_info:
        await provider.gate(MagicMock(), [])

    assert exc_info.value.state == "waiting_for_user"
    browser.elevate_to_headful.assert_called_once()


# 11. Restricted policy: does NOT auto-elevate
@pytest.mark.anyio
async def test_restricted_policy():
    browser = MagicMock(spec=BrowserService)
    browser.is_currently_headless = True
    browser.inspect = AsyncMock(return_value="restricted")
    browser.elevate_to_headful = AsyncMock()

    provider = XiaohongshuProvider(browser=browser, auto_elevate=True)
    with pytest.raises(UserActionRequired) as exc_info:
        await provider.gate(MagicMock(), [])

    assert exc_info.value.state == "waiting_for_user"
    # Restricted does not auto-elevate
    browser.elevate_to_headful.assert_not_called()

    # Same for Douyin
    dy_browser = MagicMock(spec=DouyinBrowserService)
    dy_browser.is_currently_headless = True
    dy_browser.inspect = AsyncMock(return_value="restricted")
    dy_browser.elevate_to_headful = AsyncMock()

    dy_provider = DouyinProvider(browser=dy_browser, auto_elevate=True)
    with pytest.raises(UserActionRequired):
        await dy_provider.gate(MagicMock(), [])

    dy_browser.elevate_to_headful.assert_not_called()


# 12. wait_for_user pauses search timeout
@pytest.mark.anyio
async def test_wait_for_user_pauses_timeout(tmp_path: Path):
    sessions = sessionmaker(bind=engine)
    service = SearchService(sessions=sessions)

    mock_deadline = MagicMock()
    mock_deadline.when.return_value = 1000.0
    service.deadlines["job_123"] = mock_deadline

    with patch("asyncio.get_running_loop") as mock_loop:
        mock_loop.return_value.time.return_value = 900.0  # 100s remaining

        gate = UserActionRequired("waiting_for_user", "Need captcha")

        # Start wait_for_user in background
        wait_task = asyncio.create_task(service.wait_for_user("job_123", gate))
        await asyncio.sleep(0.01)

        # Deadline was paused
        mock_deadline.reschedule.assert_called_with(None)

        # Resume job
        service.resume("job_123")
        await wait_task

        # Deadline was restored with remaining time
        assert mock_deadline.reschedule.call_count >= 2
        last_reschedule_arg = mock_deadline.reschedule.call_args[0][0]
        # Should be scheduled around 900 + 100 = 1000.0
        assert last_reschedule_arg == pytest.approx(1000.0, rel=1e-2)


# 13. Resume transition: waiting -> resuming -> searching
@pytest.mark.anyio
async def test_resume_transition_waiting_resuming_searching(tmp_path: Path):
    from uuid import uuid4

    sessions = sessionmaker(bind=engine)
    service = SearchService(sessions=sessions)
    job_id = f"job_trans_{uuid4()}"

    with sessions() as db:
        job = SearchJob(
            id=job_id,
            original_query="test",
            platforms=["xiaohongshu"],
            requested_limit=10,
            status="pending",
        )
        db.add(job)
        db.commit()

    gate = UserActionRequired("waiting_for_user", "Verification required")

    wait_task = asyncio.create_task(service.wait_for_user(job_id, gate))
    await asyncio.sleep(0.02)

    with sessions() as db:
        j = db.get(SearchJob, job_id)
        assert j.status == "waiting_for_user"

    # Call resume
    resumed = service.resume(job_id)
    assert resumed is True

    # At resume call, status becomes resuming
    with sessions() as db:
        j = db.get(SearchJob, job_id)
        assert j.status in {"resuming", "searching"}

    await wait_task

    # After wait_for_user completes, status transitions to searching
    with sessions() as db:
        j = db.get(SearchJob, job_id)
        assert j.status == "searching"


# 14. Cancellation while waiting
@pytest.mark.anyio
async def test_cancellation_while_waiting():
    from uuid import uuid4

    sessions = sessionmaker(bind=engine)
    service = SearchService(sessions=sessions)
    job_id = f"job_cancel_{uuid4()}"

    with sessions() as db:
        job = SearchJob(
            id=job_id,
            original_query="test",
            platforms=["xiaohongshu"],
            requested_limit=10,
            status="pending",
        )
        db.add(job)
        db.commit()

    gate = UserActionRequired("waiting_for_user", "Verification required")

    wait_task = asyncio.create_task(service.wait_for_user(job_id, gate))
    service.tasks[job_id] = wait_task
    await asyncio.sleep(0.02)

    await service.cancel(job_id)

    with sessions() as db:
        j = db.get(SearchJob, job_id)
        assert j.status == "cancelled"

    assert job_id not in service.waiters


# 15. Browser lock prevents BrowserMediaResolver concurrency conflict
@pytest.mark.anyio
async def test_browser_lock_prevents_resolver_concurrency():
    xhs_browser = MagicMock()
    xhs_browser.lock = asyncio.Lock()
    mock_page = MagicMock()
    mock_page.is_closed.return_value = False
    mock_page.evaluate = AsyncMock(return_value={"mediaUrl": "https://sns-video-bd.xhscdn.com/video123"})
    xhs_browser.get_active_page.return_value = mock_page

    resolver = BrowserMediaResolver(xhs_browser_service=xhs_browser)
    video = Video(
        id="vid_1",
        platform="xiaohongshu",
        platform_video_id="661234567890",
        canonical_url="https://www.xiaohongshu.com/explore/661234567890",
        search_query="test",
        search_job_id="job_1",
    )

    events: list[str] = []

    # Simulate active search holding the browser lock
    async def simulated_search():
        async with xhs_browser.lock:
            events.append("search_started")
            await asyncio.sleep(0.05)
            events.append("search_ended")

    # Resolver trying to resolve concurrently
    async def simulated_download():
        await asyncio.sleep(0.01)  # start slightly after search
        events.append("download_trying_lock")
        resolved = await resolver.resolve(video)
        events.append("download_finished")
        return resolved

    search_task = asyncio.create_task(simulated_search())
    download_task = asyncio.create_task(simulated_download())

    await asyncio.gather(search_task, download_task)

    # Assert strict serialization: download must not evaluate until search released lock
    assert events == ["search_started", "download_trying_lock", "search_ended", "download_finished"]


# 16. Manual API guards during search
def test_manual_api_guards_during_search():
    with TestClient(app) as client:
        xhs_browser = app.state.browser
        # Simulate active lock
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(xhs_browser.lock.acquire())
            assert xhs_browser.lock.locked() is True

            # Manual click should return 409
            res_click = client.post("/api/browser/xiaohongshu/click", json={"x": 100, "y": 200})
            assert res_click.status_code == 409
            assert "tìm kiếm" in res_click.json()["detail"]

            # Manual drag should return 409
            res_drag = client.post(
                "/api/browser/xiaohongshu/drag",
                json={"from_x": 10, "from_y": 10, "to_x": 50, "to_y": 50},
            )
            assert res_drag.status_code == 409

            # Refresh QR should return 409
            res_qr = client.post("/api/browser/xiaohongshu/refresh_qr")
            assert res_qr.status_code == 409

            # Open browser should return 409
            res_open = client.post("/api/browser/xiaohongshu/open")
            assert res_open.status_code == 409

            # Close browser should return 409
            res_close = client.post("/api/browser/xiaohongshu/close")
            assert res_close.status_code == 409
        finally:
            xhs_browser.lock.release()
            loop.close()


# 17. Partial results survive waiting/resume
@pytest.mark.anyio
async def test_partial_results_survive_waiting_resume():
    from uuid import uuid4

    Base.metadata.create_all(engine)
    sessions = sessionmaker(bind=engine)

    # Prepare job
    job_id = f"job_partial_test_{uuid4()}"
    with sessions() as db:
        job = SearchJob(
            id=job_id,
            original_query="keyboard test",
            platforms=["xiaohongshu"],
            requested_limit=5,
            is_mock=False,
            status="pending",
        )
        db.add(job)
        db.commit()

    v1_id = f"66{uuid4().int % 10000000000:010d}"
    v2_id = f"66{(uuid4().int + 1) % 10000000000:010d}"

    item1 = VideoResult(
        platform="xiaohongshu",
        platform_video_id=v1_id,
        url=f"https://www.xiaohongshu.com/explore/{v1_id}",
        title="Custom Mechanical Keyboard 1",
        author_name="Geek1",
        like_count=100,
    )
    item2 = VideoResult(
        platform="xiaohongshu",
        platform_video_id=v2_id,
        url=f"https://www.xiaohongshu.com/explore/{v2_id}",
        title="Custom Mechanical Keyboard 2",
        author_name="Geek2",
        like_count=200,
    )

    call_count = 0

    class MockControlledProvider:
        async def search(self, query, limit, filters, on_batch=None):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                # Scrapes 1 item, then encounters gate
                raise UserActionRequired("waiting_for_user", "Captcha triggered", partial=[item1])
            # Call 2: user solved captcha, returns item 1 (already seen) and item 2
            return [item1, item2]

        async def close(self):
            pass

    service = SearchService(
        sessions=sessions,
        factory=lambda platform, mock: MockControlledProvider(),
    )

    pipeline_task = asyncio.create_task(service.pipeline(job_id))
    for _ in range(20):
        await asyncio.sleep(0.05)
        with sessions() as db:
            j = db.get(SearchJob, job_id)
            if j and j.status == "waiting_for_user":
                break

    # Job is waiting for user
    with sessions() as db:
        j = db.get(SearchJob, job_id)
        assert j.status == "waiting_for_user"
        # Partial item1 is already persisted in DB!
        assert j.processed_count == 1

    # User resumes
    service.resume(job_id)
    await pipeline_task

    # Job completed
    with sessions() as db:
        j = db.get(SearchJob, job_id)
        assert j.status == "completed"
        # Total processed count is 2 (no duplicate insertion of item 1!)
        assert j.processed_count == 2
        # Duplicate count counted for item1 on second pass
        assert j.duplicate_count >= 1

        links = db.scalars(select(JobVideo).where(JobVideo.job_id == job_id)).all()
        assert len(links) == 2


def test_snapshots_include_login_url(tmp_path: Path):
    xhs = BrowserService(profile=tmp_path)
    xhs_snap = xhs.snapshot()
    assert "login_url" in xhs_snap
    assert "xiaohongshu.com" in xhs_snap["login_url"] or "rednote.com" in xhs_snap["login_url"]

    douyin = DouyinBrowserService(profile=tmp_path)
    dy_snap = douyin.snapshot()
    assert "login_url" in dy_snap
    assert "douyin.com" in dy_snap["login_url"]


