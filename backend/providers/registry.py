from backend.core.config import settings
from backend.providers.base import ProviderUnavailableError, SearchProvider
from backend.providers.douyin.provider import DouyinProvider
from backend.providers.mock.provider import MockProvider
from backend.providers.tiktok.provider import TikTokSearchProvider
from backend.providers.xiaohongshu.provider import XiaohongshuProvider
from backend.providers.youtube_shorts.provider import YouTubeShortsSearchProvider
from backend.schemas.contracts import Platform
from backend.services.browser_service import BrowserService
from backend.services.douyin_browser_service import DouyinBrowserService


def get_provider(
    platform: Platform,
    use_mock: bool,
    browser: BrowserService | None = None,
    douyin_browser: DouyinBrowserService | None = None,
    auto_elevate: bool | None = None,
) -> SearchProvider:
    effective_auto_elevate = (
        auto_elevate if auto_elevate is not None else getattr(settings, "browser_auto_elevate", True)
    )
    if platform in ("tiktok", "youtube_shorts"):
        if not use_mock:
            raise ProviderUnavailableError(
                f"Provider thật cho {platform} hiện đang phát triển. Vui lòng chuyển sang chế độ Dữ liệu mẫu (Mock)."
            )
        if platform == "tiktok":
            return TikTokSearchProvider(browser_service=browser, is_mock=use_mock)
        return YouTubeShortsSearchProvider(browser_service=browser, is_mock=use_mock)
    if use_mock:
        return MockProvider(platform)
    if platform == "xiaohongshu" and browser:
        return XiaohongshuProvider(browser, auto_elevate=effective_auto_elevate)
    if platform == "douyin" and douyin_browser:
        return DouyinProvider(douyin_browser, auto_elevate=effective_auto_elevate)
    if platform == "mock":
        return MockProvider("douyin")
    raise ProviderUnavailableError(
        f"Provider thật cho {platform} chưa sẵn sàng hoặc trình duyệt chưa khởi tạo."
    )
