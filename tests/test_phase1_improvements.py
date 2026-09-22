import asyncio
import os
from unittest.mock import AsyncMock, MagicMock, patch

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend.core.database import Base
from backend.models import SearchJob
from backend.providers.base import UserActionRequired
from backend.services.dubbing.audio_mixer import resolve_ffmpeg_binary
from backend.services.search_service import SearchService


def test_portable_ffmpeg_resolution_priority(tmp_path):
    # Test 1: explicit path provided
    custom_bin = tmp_path / "custom_ffmpeg.exe"
    custom_bin.write_text("dummy")
    assert resolve_ffmpeg_binary(str(custom_bin)) == str(custom_bin)

    # Test 2: portable bin directory takes priority over system PATH
    fake_root = tmp_path / "app"
    fake_bin = fake_root / "bin"
    fake_bin.mkdir(parents=True)
    local_name = "ffmpeg.exe" if os.name == "nt" else "ffmpeg"
    portable_ffmpeg = fake_bin / local_name
    portable_ffmpeg.write_text("binary")

    with patch("backend.services.dubbing.audio_mixer.ROOT", fake_root):
        with patch("os.access", return_value=True):
            resolved = resolve_ffmpeg_binary()
            assert resolved == str(portable_ffmpeg)

    # Test 3: fallback to system PATH when local bin does not exist
    with patch("backend.services.dubbing.audio_mixer.ROOT", tmp_path / "nonexistent"):
        with patch("shutil.which", return_value="/usr/bin/ffmpeg"):
            resolved = resolve_ffmpeg_binary()
            assert resolved == "/usr/bin/ffmpeg"


def test_search_service_auto_resume_on_captcha_solve():
    async def scenario():
        engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(engine)
        sessions = sessionmaker(engine, expire_on_commit=False)

        with sessions() as db:
            job = SearchJob(original_query="test", platforms=["douyin"], requested_limit=5, is_mock=False)
            db.add(job)
            db.commit()
            job_id = job.id

        service = SearchService(sessions)

        # Mock provider and browser
        mock_page = MagicMock()
        mock_page.is_closed = MagicMock(return_value=False)
        mock_page.url = "https://www.douyin.com/search/test"

        mock_browser = MagicMock()
        mock_browser.get_active_page = MagicMock(return_value=mock_page)

        # State sequence: verification_required at first, then becomes page_available
        states = ["verification_required", "page_available"]

        async def mock_inspect(page):
            if states:
                return states.pop(0)
            return "page_available"

        mock_browser.inspect = AsyncMock(side_effect=mock_inspect)

        mock_provider = MagicMock()
        mock_provider.browser = mock_browser

        gate = UserActionRequired("verification_required", "CAPTCHA detected")

        # Mock asyncio.sleep to not wait real seconds
        real_sleep = asyncio.sleep

        async def fast_sleep(s):
            await real_sleep(0.01)

        with patch("asyncio.sleep", fast_sleep):
            # wait_for_user should auto-resume when inspect returns page_available
            await service.wait_for_user(job_id, gate, provider=mock_provider)

        with sessions() as db:
            updated_job = db.get(SearchJob, job_id)
            assert updated_job is not None
            assert updated_job.status in ("searching", "resuming")
            assert updated_job.error_message is None

    asyncio.run(scenario())
