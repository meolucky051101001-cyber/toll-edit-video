import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from pydantic import SecretStr
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend.core.config import Settings
from backend.core.database import Base
from backend.models.download_job import DownloadJob
from backend.models.video import Video
from backend.providers.ai.gemini import GeminiAIProvider
from backend.services.download.base import BaseDownloadAdapter
from backend.services.download.service import DownloadService
from backend.services.ranking_service import compute_hybrid_score, cosine_similarity


def test_cosine_similarity():
    # Identical vectors
    assert abs(cosine_similarity([1.0, 2.0, 3.0], [1.0, 2.0, 3.0]) - 1.0) < 1e-5
    # Orthogonal vectors
    assert abs(cosine_similarity([1.0, 0.0], [0.0, 1.0]) - 0.0) < 1e-5
    # Parallel scaled
    assert abs(cosine_similarity([1.0, 1.0], [5.0, 5.0]) - 1.0) < 1e-5
    # Empty or mismatched
    assert cosine_similarity([], [1.0]) == 0.0
    assert cosine_similarity([1.0, 2.0], [1.0]) == 0.0
    assert cosine_similarity([0.0, 0.0], [0.0, 0.0]) == 0.0


def test_compute_hybrid_score():
    # When semantic similarity is present: 0.5 * (0.8 * 100) + 0.3 * 70 + 0.2 * 80
    # = 40 + 21 + 16 = 77.0
    hybrid = compute_hybrid_score(lexical_relevance=70.0, semantic_similarity=0.8, quality_score=80.0)
    assert hybrid == 77.0

    # Fallback to standard formula when semantic similarity is None
    # 70 * (0.8 + 0.2 * 80 / 100) = 70 * (0.8 + 0.16) = 70 * 0.96 = 67.2
    standard = compute_hybrid_score(lexical_relevance=70.0, semantic_similarity=None, quality_score=80.0)
    assert standard == 67.2


def test_gemini_embed_and_ocr_methods():
    async def scenario():
        config = Settings(
            ai_api_key=SecretStr("test_key"),
            ai_free_tier_confirmed=True,
            ai_model="gemini-3.1-flash-lite",
        )
        provider = GeminiAIProvider(config)

        # 1. Test embed_text
        mock_embed_resp = SimpleNamespace(
            status_code=200,
            json=lambda: {"embedding": {"values": [0.12, -0.34, 0.56]}},
        )
        with patch("httpx.AsyncClient.post", new_callable=AsyncMock) as mock_post:
            mock_post.return_value = mock_embed_resp
            vec = await provider.embed_text("decor ban hoc")
            assert vec == [0.12, -0.34, 0.56]

        # 2. Test extract_image_text
        mock_ocr_resp = SimpleNamespace(
            status_code=200,
            json=lambda: {
                "candidates": [
                    {
                        "content": {
                            "parts": [{"text": "这是一个手帐测评文具清单"}]
                        }
                    }
                ]
            },
        )
        with patch("httpx.AsyncClient.post", new_callable=AsyncMock) as mock_post:
            mock_post.return_value = mock_ocr_resp
            ocr_text = await provider.extract_image_text(b"fake_image_bytes")
            assert "手帐测评文具清单" in ocr_text

    asyncio.run(scenario())


def test_download_service_carousel_ocr_enrichment(tmp_path):
    async def scenario():
        engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(engine)
        sessions = sessionmaker(engine, expire_on_commit=False)

        video_id = "v1001"
        with sessions() as db:
            video = Video(
                id=video_id,
                platform="douyin",
                platform_video_id="7410000000000000001",
                url="https://www.douyin.com/note/7410000000000000001",
                canonical_url="https://www.douyin.com/note/7410000000000000001",
                fingerprint="fp_7410000000000000001",
                search_query="test note",
                search_job_id="job_test_1001",
                title="Douyin Note Test",
                media_type="image_text",
                images=["https://cdn.example.com/1.jpg", "https://cdn.example.com/2.jpg"],
                script_analysis={"existing_field": 123},
            )
            db.add(video)
            db.commit()

        # Mock AI service
        mock_ai_provider = MagicMock()
        mock_ai_provider.extract_image_text = AsyncMock(return_value="OCR Extracted Heading")
        mock_ai_service = SimpleNamespace(
            factory=lambda cfg: mock_ai_provider,
            config=SimpleNamespace(),
        )

        dl_dir = tmp_path / "downloads"
        dl_dir.mkdir(parents=True, exist_ok=True)

        class DummyAdapter(BaseDownloadAdapter):
            name = "dummy"
            version = "1.0"
            async def health_check(self): return True
            async def resolve(self, v): raise NotImplementedError

        service = DownloadService(
            sessions,
            xhs_adapter=DummyAdapter(),
            douyin_adapter=DummyAdapter(),
            download_dir=dl_dir,
            ai_service=mock_ai_service,
        )

        with sessions() as db:
            job = DownloadJob(
                video_id=video_id,
                platform="douyin",
                status="downloading",
            )
            db.add(job)
            db.commit()
            job_id = job.id

        with sessions() as db:
            v_obj = db.get(Video, video_id)
            assert v_obj is not None

            # Mock successful image download
            mock_client = AsyncMock()
            mock_client.get = AsyncMock(return_value=SimpleNamespace(status_code=200, content=b"fake_jpg_content"))
            class MockSafeClientContext:
                async def __aenter__(self):
                    return mock_client
                async def __aexit__(self, *args):
                    pass

            with patch("backend.services.download.service.create_safe_async_client", return_value=MockSafeClientContext()), \
                 patch("backend.services.download.service.is_safe_media_url_async", return_value=True):
                result = await service._download_image_album(job_id, v_obj)
                assert result == "completed"

        # Check that OCR text was saved into video.script_analysis
        with sessions() as db:
            v_reloaded = db.get(Video, video_id)
            assert v_reloaded is not None
            assert v_reloaded.script_analysis is not None
            assert v_reloaded.script_analysis.get("ocr_text") == "OCR Extracted Heading"

    asyncio.run(scenario())
