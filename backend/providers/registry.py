from backend.providers.base import ProviderUnavailableError, SearchProvider
from backend.providers.douyin.provider import DouyinProvider
from backend.providers.mock.provider import MockProvider
from backend.providers.xiaohongshu.provider import XiaohongshuProvider
from backend.schemas.contracts import Platform
from backend.services.browser_service import BrowserService
from backend.services.douyin_browser_service import DouyinBrowserService


def get_provider(
    platform: Platform,
    use_mock: bool,
    browser: BrowserService | None = None,
    douyin_browser: DouyinBrowserService | None = None,
) -> SearchProvider:
    if use_mock:
        return MockProvider(platform)
    if platform == "xiaohongshu" and browser:
        return XiaohongshuProvider(browser)
    if platform == "douyin" and douyin_browser:
        return DouyinProvider(douyin_browser)
    raise ProviderUnavailableError(
        f"Provider thật cho {platform} chưa sẵn sàng hoặc trình duyệt chưa khởi tạo."
    )
