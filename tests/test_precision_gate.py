import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from backend.core.database import Base
from backend.models import JobVideo, SearchJob, Video
from backend.providers.base import SearchProvider
from backend.schemas.contracts import Platform, SearchFilters, VideoResult
from backend.services.search_service import SearchService


class PrecisionMockProvider(SearchProvider):
    def __init__(self, platform: Platform = "xiaohongshu"):
        self.platform = platform

    async def search(self, query: str, limit: int, filters: SearchFilters, on_batch=None):
        return [
            # Relevant candidate: matches nhíp AND sticker
            VideoResult(
                platform=self.platform,
                platform_video_id="good-vid-1",
                url="https://www.xiaohongshu.com/explore/goodvid1",
                title="贴纸专用镊子 手账咕卡精密弯头",
                caption="nhíp gắp sticker chuyên dụng",
            ),
            # Irrelevant candidate: only sticker, missing nhíp
            VideoResult(
                platform=self.platform,
                platform_video_id="bad-vid-missing-intent",
                url="https://www.xiaohongshu.com/explore/badvid2",
                title="可爱贴纸开箱 咕卡手账好物",
                caption="chỉ có sticker",
            ),
            # Totally irrelevant candidate: random topic
            VideoResult(
                platform=self.platform,
                platform_video_id="bad-vid-low-relevance",
                url="https://www.xiaohongshu.com/explore/badvid3",
                title="做饭日常 一人食家常菜",
                caption="nấu ăn hàng ngày",
            ),
        ]


@pytest.mark.anyio
async def test_precision_gate_quarantines_irrelevant_videos():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine, expire_on_commit=False)

    provider = PrecisionMockProvider()
    service = SearchService(
        sessions=sessions,
        factory=lambda p, mock: provider,
    )

    with sessions() as db:
        job = SearchJob(
            id="job-prec-1",
            original_query="nhíp gắp sticker",
            platforms=["xiaohongshu"],
            requested_limit=5,
            status="pending",
            is_mock=False,
            min_relevance=45.0,
        )
        db.add(job)
        db.commit()

    await service.pipeline("job-prec-1")

    with sessions() as db:
        job = db.get(SearchJob, "job-prec-1")
        assert job is not None
        assert job.status == "completed"

        # Only the good video should be accepted
        assert job.processed_count == 1
        assert job.found_count == 3
        assert job.rejected_count == 2

        # Check diagnostics pool
        assert job.diagnostics is not None
        rejected = job.diagnostics.get("rejected_candidates", [])
        assert len(rejected) == 2
        rejected_ids = [r["video_id"] for r in rejected]
        assert "bad-vid-missing-intent" in rejected_ids
        assert "bad-vid-low-relevance" in rejected_ids

        # Verify only good video exists in JobVideo and Video tables
        jv_list = db.scalars(select(JobVideo).where(JobVideo.job_id == "job-prec-1")).all()
        assert len(jv_list) == 1

        vids = db.scalars(select(Video)).all()
        assert len(vids) == 1
        assert vids[0].platform_video_id == "good-vid-1"
