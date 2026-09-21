import hashlib
import logging
from collections.abc import Awaitable, Callable

from backend.providers.base import ProviderUnavailableError, SearchFilters, SearchProvider
from backend.schemas.contracts import VideoResult

logger = logging.getLogger("provider.tiktok")


class TikTokSearchProvider(SearchProvider):
    """Search provider for TikTok Global."""

    def __init__(self, browser_service=None, is_mock: bool = False):
        self.browser = browser_service
        self.is_mock = is_mock

    async def search(
        self,
        query: str,
        limit: int,
        filters: SearchFilters,
        on_batch: Callable[[list[VideoResult]], Awaitable[None]] | None = None,
    ) -> list[VideoResult]:
        logger.info("Searching TikTok for query: %s (limit: %d, mock: %s)", query, limit, self.is_mock)
        if not self.is_mock:
            raise ProviderUnavailableError(
                "Tìm kiếm trực tiếp trên TikTok hiện chưa sẵn sàng parser dữ liệu thật. "
                "Vui lòng sử dụng chế độ Dữ liệu mẫu (Mock)."
            )

        results: list[VideoResult] = []
        for idx in range(limit):
            seed = f"tiktok_{query}_{idx}"
            h = hashlib.sha256(seed.encode("utf-8")).hexdigest()
            vid_id = str(int(h[:15], 16))[:19].ljust(19, "0")
            author = f"creator_{h[:6]}"

            vr = VideoResult(
                platform="tiktok",
                platform_video_id=vid_id,
                url=f"https://www.tiktok.com/@{author}/video/{vid_id}",
                title=f"{query} review & unbox #{idx + 1}",
                caption=f"Mẹo hay và trải nghiệm thực tế với {query} cực chuẩn #fyp #trending",
                author_name=f"TikToker {author}",
                author_id=author,
                author_url=f"https://www.tiktok.com/@{author}",
                hashtags=[query.replace(" ", ""), "fyp", "trending", "tiktok"],
                duration_seconds=float(15 + (idx * 5) % 60),
                like_count=10000 + int(h[:4], 16) * 5,
                comment_count=500 + int(h[4:7], 16),
                share_count=200 + int(h[7:9], 16),
                favorite_count=1500 + int(h[9:12], 16),
                view_count=50000 + int(h[:5], 16) * 10,
                is_mock=self.is_mock,
            )
            results.append(vr)

        if on_batch:
            await on_batch(results)

        return results

    async def close(self) -> None:
        pass
