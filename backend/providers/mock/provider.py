import asyncio
import hashlib
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta, timezone

from backend.providers.base import SearchProvider
from backend.schemas.contracts import Platform, SearchFilters, VideoResult

TOPICS = [
    (
        "Unbox đồ cute · một góc nhỏ xinh",
        "unbox đồ cute mở hộp đồ dùng nhỏ xinh",
        ["开箱", "好物分享"],
    ),
    (
        "Sticker & sổ tay · thử một set mới",
        "dụng cụ bóc sticker cute trang trí sổ tay",
        ["手帐", "贴纸工具"],
    ),
    (
        "Desk reset · decor bàn học",
        "đồ decor bàn học cute sắp xếp góc học tập",
        ["桌面布置", "桌面收纳"],
    ),
    ("BJD diary · chi tiết tí hon", "BJD makeup búp bê phụ kiện thủ công", ["BJD", "手作"]),
    ("DIY · một món quà nhỏ", "DIY làm quà thủ công cute unbox đồ cute", ["DIY", "可爱文具"]),
    (
        "Everyday finds · văn phòng phẩm",
        "văn phòng phẩm đồ dùng học tập cute",
        ["文具分享", "好物"],
    ),
]


class MockProvider(SearchProvider):
    def __init__(self, platform: Platform, delay: float = 0.25):
        self.platform = platform
        self.delay = delay

    async def search(
        self,
        query: str,
        limit: int,
        filters: SearchFilters,
        on_batch: Callable[[list[VideoResult]], Awaitable[None]] | None = None,
    ) -> list[VideoResult]:
        await asyncio.sleep(self.delay)
        offset = int(hashlib.sha256(query.encode()).hexdigest()[:6], 16) % len(TOPICS)
        results = []
        for i in range(limit):
            index = (i + offset) % len(TOPICS)
            title, caption, tags = TOPICS[index]
            number = i // len(TOPICS) + 1
            item = VideoResult(
                platform=self.platform,
                platform_video_id=f"mock-{index}-{number}",
                url=f"https://example.invalid/{self.platform}/mock-{index}-{number}",
                title=f"{title} / {number:02d}",
                caption=caption + " — Dữ liệu mẫu để thử công cụ.",
                author_name=["Mây Studio", "Little Things", "Góc của Linh"][i % 3],
                author_id=f"mock-author-{i % 3}",
                hashtags=tags,
                keywords=caption.split(),
                duration_seconds=18 + i * 3,
                like_count=120 + (i * 719) % 18000,
                comment_count=5 + i * 7,
                favorite_count=30 + i * 23,
                published_at=datetime(2026, 8, 30, tzinfo=timezone.utc) - timedelta(days=i),
                is_mock=True,
            )
            if (item.like_count or 0) >= filters.minimum_likes:
                results.append(item)
        # Deliberate overlap tests the real dedup path; sample data is never presented as live.
        return results + results[: min(3, len(results))]
