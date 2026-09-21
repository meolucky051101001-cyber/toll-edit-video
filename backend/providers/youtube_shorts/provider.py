import hashlib
import logging
from collections.abc import Awaitable, Callable

from backend.providers.base import ProviderUnavailableError, SearchFilters, SearchProvider
from backend.schemas.contracts import VideoResult

logger = logging.getLogger("provider.youtube_shorts")


class YouTubeShortsSearchProvider(SearchProvider):
    """Search provider for YouTube Shorts (#shorts)."""

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
        logger.info("Searching YouTube Shorts for query: %s (limit: %d, mock: %s)", query, limit, self.is_mock)
        if not self.is_mock:
            raise ProviderUnavailableError(
                "Tìm kiếm trực tiếp trên YouTube Shorts hiện chưa sẵn sàng parser dữ liệu thật. "
                "Vui lòng sử dụng chế độ Dữ liệu mẫu (Mock)."
            )

        results: list[VideoResult] = []

        # Generate realistic, query-aligned YouTube Shorts
        chars = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-"
        for idx in range(limit):
            seed = f"yt_shorts_{query}_{idx}"
            h = hashlib.sha256(seed.encode("utf-8")).hexdigest()
            # Construct standard 11-char YouTube ID
            vid_id = "".join(chars[int(h[i * 2 : i * 2 + 2], 16) % len(chars)] for i in range(11))
            channel = f"channel_{h[:5]}"

            vr = VideoResult(
                platform="youtube_shorts",
                platform_video_id=vid_id,
                url=f"https://www.youtube.com/shorts/{vid_id}",
                title=f"Bí quyết {query} siêu nhanh trong 30s #shorts",
                caption=f"Hướng dẫn chi tiết cách làm {query} dễ nhất dành cho bạn #shorts #viral",
                author_name=f"YouTuber {channel.upper()}",
                author_id=channel,
                author_url=f"https://www.youtube.com/@{channel}",
                hashtags=[query.replace(" ", ""), "shorts", "viral", "youtube"],
                duration_seconds=float(20 + (idx * 7) % 40),
                like_count=5000 + int(h[:4], 16) * 3,
                comment_count=200 + int(h[4:7], 16),
                share_count=100 + int(h[7:9], 16),
                favorite_count=800 + int(h[9:12], 16),
                view_count=35000 + int(h[:5], 16) * 8,
                is_mock=self.is_mock,
            )
            results.append(vr)

        if on_batch:
            await on_batch(results)

        return results

    async def close(self) -> None:
        pass
