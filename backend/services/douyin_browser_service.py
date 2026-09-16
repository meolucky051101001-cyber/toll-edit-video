import asyncio
import json
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from playwright.async_api import BrowserContext, Error, Page, Playwright, async_playwright

from backend.core.config import ROOT, settings
from backend.providers.base import ProviderUnavailableError
from backend.providers.douyin.parser import classify_page

DOUYIN_HOME = "https://www.douyin.com"
MESSAGES = {
    "closed": "Trình duyệt Douyin chưa mở.",
    "page_available": "Trang Douyin đã mở. Sẵn sàng tìm kiếm.",
    "login_required": "Douyin yêu cầu đăng nhập tài khoản. Hãy bấm vào đường link bên cạnh để đăng nhập trên trình duyệt, rồi bấm Tiếp tục lượt tìm.",
    "verification_required": "Douyin yêu cầu xác minh bảo mật. Hãy mở liên kết để hoàn tất xác minh, rồi bấm Tiếp tục lượt tìm.",
    "restricted": "Douyin đang hạn chế truy cập / IP hoặc hệ thống bận. Lượt tìm đã dừng; thử lại sau.",
    "network_error": "Không kết nối được tới Douyin. Vui lòng kiểm tra kết nối mạng.",
    "busy": "Trình duyệt Douyin đang xử lý lượt tìm.",
}
HEAVY_EXTENSIONS = (
    ".mp4",
    ".m4s",
    ".m3u8",
    ".ts",
    ".webm",
    ".flv",
    ".avi",
    ".woff",
    ".woff2",
    ".ttf",
)


class DouyinBrowserService:
    def __init__(self, profile: Path | None = None, headless: bool = True, hybrid_headless: bool = True):
        self.profile = profile or ROOT / "data/browser/douyin"
        self.headless = headless
        self.hybrid_headless = hybrid_headless
        self.is_currently_headless = False
        self.driver: Playwright | None = None
        self.context: BrowserContext | None = None
        self.page: Page | None = None
        self.search_page: Page | None = None
        self.blocked_page: Page | None = None
        self.lock = asyncio.Lock()
        self.state = "closed"
        self.base_url = DOUYIN_HOME
        self.diagnostics: dict = {}

    def should_run_headless(self) -> bool:
        if self.headless:
            return True
        if not getattr(self, "hybrid_headless", True):
            return False
        auth_file = self.profile / "auth_state.json"
        if auth_file.exists():
            try:
                data = json.loads(auth_file.read_text("utf-8"))
                cookies = data.get("cookies", [])
                if cookies and (data.get("grade") in {"high_quality", "valid", "basic"} or len(cookies) >= 5):
                    return True
            except Exception:
                pass
        return False

    async def elevate_to_headful(self) -> Page:
        if not getattr(self, "is_currently_headless", False):
            return self.page or await self.ensure_open()
        current_url = self.page.url if self.page and not self.page.is_closed() else None
        await self._close()
        self.hybrid_headless = False
        page = await self.ensure_open()
        if current_url and not current_url.startswith("about:"):
            try:
                await page.goto(current_url, wait_until="domcontentloaded", timeout=15000)
            except Exception:
                pass
        await self.inspect(page)
        return page

    def get_active_page(self) -> Page:
        if self.blocked_page and not self.blocked_page.is_closed():
            return self.blocked_page
        if self.page and not self.page.is_closed():
            return self.page
        raise ProviderUnavailableError("Trình duyệt Douyin chưa mở.")

    async def release_blocked_page(self) -> None:
        if self.blocked_page:
            try:
                if not self.blocked_page.is_closed():
                    await self.blocked_page.close()
            except Exception:
                pass
            self.blocked_page = None
        if self.search_page and not self.search_page.is_closed():
            self.page = self.search_page

    def snapshot(self) -> dict:
        active = None
        try:
            active = self.get_active_page()
        except Exception:
            pass
        if self.page and self.page.is_closed():
            self.state = "closed"
        login_url = self.base_url or "https://www.douyin.com"
        return {
            "state": self.state,
            "message": MESSAGES[self.state],
            "profile_location": str(self.profile),
            "base_url": self.base_url,
            "login_url": login_url,
            "open": bool(self.page and not self.page.is_closed()),
            "busy": self.lock.locked(),
            "diagnostics": self.diagnostics,
            "active_url": active.url if active else None,
            "is_blocked_tab": bool(self.blocked_page and not self.blocked_page.is_closed()),
        }

    async def ensure_open(self) -> Page:
        if self.page and not self.page.is_closed():
            return self.page
        await self._close()
        self.profile.mkdir(parents=True, exist_ok=True)
        try:
            self.driver = await async_playwright().start()
            effective_headless = self.should_run_headless()
            self.is_currently_headless = effective_headless
            self.context = await self.driver.chromium.launch_persistent_context(
                str(self.profile),
                headless=effective_headless,
                channel=settings.xhs_browser_channel,
                viewport={"width": 1280, "height": 900},
                accept_downloads=False,
                locale="zh-CN",
                timezone_id="Asia/Shanghai",
                color_scheme="light",
                args=[
                    "--no-sandbox",
                    "--disable-infobars",
                ],
            )

            TRACKER_DOMAINS = (
                "sensorsdata.cn",
                "growingio.com",
                "hm.baidu.com",
                "google-analytics.com",
                "log.douyin.com",
                "mon.snssdk.com",
                "bds.snssdk.com",
            )

            async def block_resources(route):
                req = route.request
                url = req.url.lower()
                if (
                    req.resource_type in {"media", "font"}
                    or any(url.endswith(ext) or ext + "?" in url for ext in HEAVY_EXTENSIONS)
                    or any(domain in url for domain in TRACKER_DOMAINS)
                ):
                    await route.abort()
                else:
                    await route.continue_()

            await self.context.route("**/*", block_resources)
            # Restore saved session cookies if present
            auth_file = self.profile / "auth_state.json"
            if auth_file.exists():
                try:
                    from backend.services.cookie_service import persist_auth_cookies
                    auth_data = json.loads(auth_file.read_text("utf-8"))
                    cookies = auth_data.get("cookies", [])
                    if cookies:
                        persisted = persist_auth_cookies(cookies)
                        await self.context.add_cookies(persisted)
                except Exception:
                    pass

            # Inject custom cookie from settings if configured (skip dummy/test strings)
            if settings.douyin_cookie:
                try:
                    from backend.services.cookie_service import is_dummy_cookie, parse_cookie_str, persist_auth_cookies
                    if not is_dummy_cookie(settings.douyin_cookie):
                        tokens = parse_cookie_str(settings.douyin_cookie)
                        if tokens:
                            custom_cookies = [
                                {"name": k, "value": v, "domain": ".douyin.com", "path": "/"}
                                for k, v in tokens.items()
                            ]
                            persisted_custom = persist_auth_cookies(custom_cookies)
                            await self.context.add_cookies(persisted_custom)
                except Exception:
                    pass

            self.page = (
                self.context.pages[0] if self.context.pages else await self.context.new_page()
            )
            self.page.set_default_timeout(10000)
            await self.page.goto(self.base_url, wait_until="domcontentloaded", timeout=30000)
            await self.page.wait_for_timeout(1500)
            await self.inspect(self.page)
            return self.page
        except Error:
            try:
                await self._close()
            except Error:
                pass
            self.state = "network_error"
            raise ProviderUnavailableError(
                "Không mở được trình duyệt Douyin. Kiểm tra Chromium đã cài và đóng phiên dùng cùng profile."
            ) from None

    async def inspect(self, page: Page) -> str:
        if page.is_closed():
            self.state = "closed"
            self.diagnostics = {}
            return self.state
        try:
            url = page.url
            title = await page.title()
            body = await page.locator("body").inner_text(timeout=5000)
            self.state = classify_page(body, title, url)

            parts = urlsplit(url)
            query_param_names = list(parse_qs(parts.query).keys())

            inputs = await page.locator("input:visible").all()
            input_count = len(inputs)
            search_input = page.locator("input[data-e2e='search-input'], input.search-input, input#search-input").first
            search_val = ""
            if await search_input.count() > 0:
                try:
                    search_val = (await search_input.input_value())[:100]
                except Error:
                    search_val = ""

            # Check session cookies
            has_session_cookie = False
            if self.context:
                try:
                    c_list = await self.context.cookies()
                    c_names = {c.get("name") for c in c_list}
                    has_session_cookie = "sessionid" in c_names or "passport_csrf_token" in c_names
                except Exception:
                    pass

            # Check user login status
            user_avatar = page.locator(
                "[data-e2e='user-avatar']:visible, .avatar-wrapper:visible, .user-avatar:visible, [data-e2e='user-info']:visible"
            )
            has_user_element = ((await user_avatar.count()) > 0) or has_session_cookie
            is_authenticated = bool(has_user_element or has_session_cookie)

            # Check for verification / captcha container
            captcha_box = page.locator(
                "#captcha-verify-image, .captcha-verify-container:visible, .verify-bar-close:visible, [class*='captcha']:visible"
            )
            captcha_visible = (await captcha_box.count()) > 0
            if captcha_visible:
                self.state = "verification_required"

            # Check for dismissible login modal / mask
            login_modal = page.locator(
                "#login-panel:visible, .login-mask:visible, [class*='login-panel']:visible, [class*='login-modal']:visible"
            )
            login_modal_visible = (await login_modal.count()) > 0

            # Automatically dismiss guest login popup if present
            if login_modal_visible and not captcha_visible:
                try:
                    close_btn = page.locator(
                        ".login-panel-close:visible, .dy-account-close:visible, [data-e2e='close-icon']:visible, #login-panel [class*='close']:visible, .login-mask [class*='close']:visible, [aria-label='关闭']:visible"
                    ).first
                    if await close_btn.count() > 0:
                        await close_btn.click(timeout=800)
                        await page.wait_for_timeout(300)
                    else:
                        await page.keyboard.press("Escape")
                        await page.wait_for_timeout(300)
                    login_modal_visible = (await login_modal.count()) > 0
                except Exception:
                    pass

            if self.state in {"verification_required", "restricted"}:
                pass
            elif is_authenticated:
                self.state = "page_available"
            elif "/login" in parts.path:
                self.state = "login_required"
            elif login_modal_visible:
                cards = await page.locator("a[href*='/video/'], [class*='search-result-card']").count()
                if cards > 0:
                    self.state = "page_available"
                else:
                    self.state = "login_required"
            else:
                self.state = "page_available"

            # Persist session state whenever context exists with active cookies
            if self.context:
                try:
                    c_list = await self.context.cookies()
                    if c_list:
                        auth_file = self.profile / "auth_state.json"
                        await self.context.storage_state(path=str(auth_file))
                        try:
                            from backend.services.cookie_service import persist_auth_cookies
                            raw_auth = json.loads(auth_file.read_text("utf-8"))
                            raw_auth["cookies"] = persist_auth_cookies(raw_auth.get("cookies", []))
                            auth_file.write_text(json.dumps(raw_auth, indent=2), encoding="utf-8")
                        except Exception:
                            pass
                except Exception:
                    pass

            note_links = await page.locator('a[href*="/video/"]').count()
            video_count = await page.locator("video").count()

            self.diagnostics = {
                "host": parts.netloc,
                "path": parts.path,
                "query_param_names": query_param_names,
                "title": title[:200],
                "input_count": input_count,
                "search_input_value": search_val,
                "login_modal_visible": login_modal_visible,
                "captcha_visible": captcha_visible,
                "is_authenticated": is_authenticated,
                "result_links_count": note_links,
                "video_elements_count": video_count,
            }
        except Exception:
            self.state = "network_error"
        return self.state

    async def open(self, headful: bool = True) -> dict:
        if self.lock.locked():
            return self.snapshot()
        async with self.lock:
            if headful and getattr(self, "is_currently_headless", False):
                await self.elevate_to_headful()
            else:
                page = await self.ensure_open()
                await page.bring_to_front()
                await self.inspect(page)
        return self.snapshot()

    async def refresh_qr(self) -> dict:
        if not self.page or self.page.is_closed():
            return self.snapshot()
        try:
            refresh_btn = self.page.locator(
                ".qrcode-mask:visible, .refresh-btn:visible, :text('点击刷新'):visible, :text('刷新'):visible, [class*='refresh']:visible"
            ).first
            if await refresh_btn.count() > 0:
                await refresh_btn.click(timeout=2000)
            else:
                await self.page.reload(wait_until="domcontentloaded", timeout=20000)
            await asyncio.sleep(1)
            await self.inspect(self.page)
        except Exception:
            pass
        return self.snapshot()

    async def refresh_status(self) -> dict:
        if self.lock.locked():
            return self.snapshot()
        async with self.lock:
            active = None
            try:
                active = self.get_active_page()
            except Exception:
                pass
            if active:
                try:
                    if not active.is_closed():
                        await self.inspect(active)
                    else:
                        await self._close()
                except Exception:
                    pass
            elif self.page and self.page.is_closed():
                await self._close()
        return self.snapshot()

    async def _close(self) -> None:
        try:
            if self.context:
                try:
                    auth_file = self.profile / "auth_state.json"
                    await self.context.storage_state(path=str(auth_file))
                except Exception:
                    pass
                await self.context.close()
        finally:
            self.context = None
            self.page = None
            self.search_page = None
            self.blocked_page = None
            if self.driver:
                await self.driver.stop()
                self.driver = None
            self.state = "closed"
            self.is_currently_headless = False
            self.hybrid_headless = True

    async def close(self) -> None:
        async with self.lock:
            await self._close()
