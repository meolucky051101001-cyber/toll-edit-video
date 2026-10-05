import asyncio
from unittest.mock import patch

import pytest
from sqlalchemy import select

from backend.models import JobVideo, SearchJob
from backend.providers.base import SearchProvider
from backend.schemas.contracts import VideoResult
from backend.services.search_service import SearchService


@pytest.mark.parametrize("platform", ["douyin", "xiaohongshu"])
def test_filtered_search_scans_past_initial_distractors_and_caps_reused_results(sessions, platform):
    class Provider(SearchProvider):
        calls = []

        async def search(self, query, limit, filters, on_batch=None):
            self.calls.append(limit)
            results = []
            for i in range(limit):
                pid = str(7571064796750945200 + i) if platform == "douyin" else f"{i + 1:024x}"
                url = f"https://www.douyin.com/video/{pid}" if platform == "douyin" else f"https://www.xiaohongshu.com/explore/{pid}"
                results.append(VideoResult(
                    platform=platform, platform_video_id=pid, url=url,
                    title="做饭日常" if i < 3 else "贴纸专用镊子 手账咕卡精密弯头",
                    is_mock=False,
                ))
            if on_batch:
                await on_batch(results)
            return results

        async def close(self):
            pass

    provider = Provider()
    service = SearchService(sessions, factory=lambda *_: provider)
    # Repeat using the same library videos; quota must hold for both new and existing rows.
    for _ in range(2):
        with sessions() as db:
            job = SearchJob(original_query="nhíp gắp sticker", platforms=[platform],
                            requested_limit=3, min_relevance=40, is_mock=False)
            db.add(job)
            db.commit()
            job_id = job.id
        with patch.object(service, "prepare_queries", return_value=["贴纸镊子"]):
            asyncio.run(service.run(job_id))
        with sessions() as db:
            job = db.get(SearchJob, job_id)
            assert job.status == "completed"
            assert job.processed_count == 3
            assert job.provider_counts == {platform: 3}
            assert job.rejected_count == 3
            assert len(db.scalars(select(JobVideo).where(JobVideo.job_id == job_id)).all()) == 3
    assert provider.calls == [9, 9]
