from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable
from datetime import datetime, timezone

from backend.schemas.contracts import SearchFilters, VideoResult


class ProviderError(Exception):
    pass


class LoginRequiredError(ProviderError):
    pass


class CaptchaRequiredError(ProviderError):
    pass


class RateLimitedError(ProviderError):
    pass


class SelectorChangedError(ProviderError):
    pass


class NetworkError(ProviderError):
    pass


class ProviderTimeoutError(ProviderError):
    pass


class ProviderUnavailableError(ProviderError):
    pass


class ManualActionTimeoutError(ProviderError):
    pass


class SearchProvider(ABC):
    # Concrete live crawlers opt in; mocks/adapters are untrusted by default.
    capture_origin = "untrusted"

    @abstractmethod
    async def search(
        self,
        query: str,
        limit: int,
        filters: SearchFilters,
        on_batch: Callable[[list[VideoResult]], Awaitable[None]] | None = None,
    ) -> list[VideoResult]:
        raise NotImplementedError

    async def close(self) -> None:
        pass


class UserActionRequired(ProviderError):
    def __init__(self, state: str, message: str, partial: list[VideoResult] | None = None):
        super().__init__(message)
        self.state = state
        self.partial = partial or []


def apply_search_filters_and_sorting(
    items: list[VideoResult],
    filters: SearchFilters | None = None,
    limit: int | None = None,
) -> list[VideoResult]:
    """Applies minimum_likes, content_type, publish_time filtering and sorting to VideoResults."""
    min_likes = filters.minimum_likes if filters else 0
    wanted_content_type = filters.content_type if filters else "all"
    wanted_publish_time = filters.publish_time if filters else "all"
    wanted_sort_by = filters.sort_by if filters else "relevance"

    now = datetime.now(timezone.utc)
    max_age_seconds: float | None = None
    if wanted_publish_time == "day":
        max_age_seconds = 86400.0  # 24 hours
    elif wanted_publish_time == "week":
        max_age_seconds = 7 * 86400.0  # 7 days
    elif wanted_publish_time == "six_months":
        max_age_seconds = 182 * 86400.0  # 182 days

    candidates: list[VideoResult] = []
    seen_ids: set[str] = set()

    for vr in items:
        if not vr or not vr.platform_video_id:
            continue
        if vr.platform_video_id in seen_ids:
            continue

        if min_likes > 0 and (vr.like_count is None or vr.like_count < min_likes):
            continue

        if wanted_content_type == "video" and vr.media_type != "video":
            continue
        if wanted_content_type == "image_text" and vr.media_type != "image_text":
            continue

        if max_age_seconds is not None and vr.published_at is not None:
            age = (now - vr.published_at).total_seconds()
            if age > max_age_seconds:
                continue

        seen_ids.add(vr.platform_video_id)
        candidates.append(vr)

    # Apply sorting if requested
    if wanted_sort_by == "most_liked":
        candidates.sort(key=lambda r: (r.like_count or 0), reverse=True)
    elif wanted_sort_by == "newest":
        candidates.sort(
            key=lambda r: (r.published_at.timestamp() if r.published_at else 0.0),
            reverse=True,
        )

    if limit is not None and limit > 0:
        return candidates[:limit]
    return candidates
