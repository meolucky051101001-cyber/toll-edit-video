import asyncio
import json
import logging
from pathlib import Path
from typing import Any, cast
from urllib.parse import parse_qs, urlsplit

from playwright.async_api import BrowserContext, Error, Page, Playwright, async_playwright

from backend.core.config import ROOT, settings
from backend.providers.base import ProviderUnavailableError
from backend.providers.xiaohongshu.parser import classify_page

logger = logging.getLogger("xiaohongshu.browser")

HOME = "https://www.xiaohongshu.com/explore"
MESSAGES = {
    "closed": "Trình duyệt riêng chưa mở.",
    "page_available": "Trang đã mở. Chưa xác minh đăng nhập; nền tảng có thể yêu cầu khi tìm.",
    "login_required": "Nền tảng yêu cầu đăng nhập. Hãy bấm vào đường link bên cạnh để đăng nhập trên trình duyệt, rồi bấm Tiếp tục lượt tìm.",
    "verification_required": "Nền tảng yêu cầu xác minh bảo mật. Hãy mở liên kết để hoàn tất xác minh, rồi bấm Tiếp tục lượt tìm.",
    "restricted": "Xiaohongshu đang hạn chế truy cập/IP (mã 300012). Lượt tìm đã dừng; chỉ tiếp tục khi trang truy cập lại được.",
    "network_error": "Không tải được Xiaohongshu. Vui lòng kiểm tra kết nối mạng.",
    "busy": "Trình duyệt đang xử lý lượt tìm.",
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


class BrowserService:
    def __init__(self, profile: Path | None = None, headless: bool = True, hybrid_headless: bool = True):
        self.profile = profile or ROOT / "data/browser/xiaohongshu"
        self.headless = headless
        self.hybrid_headless = hybrid_headless
        self.default_hybrid_headless = hybrid_headless
        self.force_headful_session = False
        self.is_currently_headless = headless
        self.driver: Playwright | None = None
        self.context: BrowserContext | None = None
        self.page: Page | None = None
        self.search_page: Page | None = None
        self.blocked_page: Page | None = None
        self.lock = asyncio.Lock()
        self.state = "closed"
        self.base_url = "https://www.xiaohongshu.com"
        self.diagnostics: dict = {}
        self.last_http_status: int | None = None

    def should_run_headless(self) -> bool:
        if getattr(self, "force_headful_session", False):
            return False
        if getattr(self, "sticky_headful", False):
            return False
        if not getattr(self, "hybrid_headless", True):
            return False
        if self.headless:
            return True
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

    def reset_headful_session(self, force: bool = False) -> None:
        if force or not getattr(self, "sticky_headful", False):
            self.force_headful_session = False
            self.sticky_headful = False
            self.hybrid_headless = getattr(self, "default_hybrid_headless", True)

    async def ensure_headless_session(self) -> None:
        if getattr(self, "sticky_headful", False):
            return
        if getattr(self, "force_headful_session", False):
            self.reset_headful_session(force=True)
            if getattr(self, "is_currently_headless", True) is False and self.context:
                await self._close()
        else:
            self.reset_headful_session(force=True)

    async def elevate_to_headful(self) -> Page:
        self.force_headful_session = True
        self.sticky_headful = True
        self.hybrid_headless = False
        if not getattr(self, "is_currently_headless", False):
            return self.page or await self.ensure_open(headless_override=False)
        current_url = self.page.url if self.page and not self.page.is_closed() else None
        await self._close()
        page = await self.ensure_open(headless_override=False)
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
        raise ProviderUnavailableError("Trình duyệt Xiaohongshu chưa mở.")

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
        login_url = self.base_url or "https://www.xiaohongshu.com/explore"
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

    async def detect_base_url(self) -> str:
        if not self.context:
            return "https://www.rednote.com"
        try:
            cookies = await self.context.cookies()
            has_rednote_id = any(
                "rednote.com" in c.get("domain", "") and c.get("name") == "id_token"
                for c in cookies
            )
            has_xhs_id = any(
                "xiaohongshu.com" in c.get("domain", "") and c.get("name") == "id_token"
                for c in cookies
            )
            if has_rednote_id and not has_xhs_id:
                return "https://www.rednote.com"
            if has_xhs_id and not has_rednote_id:
                return "https://www.xiaohongshu.com"

            # If no id_token, check for web_session
            has_rednote_session = any(
                "rednote.com" in c.get("domain", "") and c.get("name") == "web_session"
                for c in cookies
            )
            has_xhs_session = any(
                "xiaohongshu.com" in c.get("domain", "") and c.get("name") == "web_session"
                for c in cookies
            )
            if has_rednote_session and not has_xhs_session:
                return "https://www.rednote.com"
            if has_xhs_session and not has_rednote_session:
                return "https://www.xiaohongshu.com"
            if any("rednote.com" in c.get("domain", "") for c in cookies):
                return "https://www.rednote.com"
        except Error:
            pass
        return "https://www.rednote.com"

    async def ensure_open(
        self,
        headless_override: bool | None = None,
        initial_url: str | None = None,
    ) -> Page:
        if self.page and not self.page.is_closed():
            return self.page
        await self._close()
        self.profile.mkdir(parents=True, exist_ok=True)
        try:
            self.driver = await async_playwright().start()
            if headless_override is not None:
                effective_headless = headless_override
            else:
                effective_headless = self.should_run_headless()
            self.is_currently_headless = effective_headless
            self.context = await self.driver.chromium.launch_persistent_context(
                str(self.profile),
                headless=effective_headless,
                channel=settings.xhs_browser_channel,
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
                viewport={"width": 1280, "height": 900},
                accept_downloads=False,
                locale="zh-CN",
                timezone_id="Asia/Shanghai",
                color_scheme="light",
                args=[
                    "--no-sandbox",
                    "--disable-infobars",
                    "--disable-blink-features=AutomationControlled",
                    "--disable-features=IsolateOrigins,site-per-process",
                    "--disable-dev-shm-usage",
                    "--no-first-run",
                    "--no-default-browser-check",
                    "--window-size=1280,900",
                ],
            )

            # Enterprise stealth injection to mask automated Playwright fingerprints
            STEALTH_INIT_SCRIPT = """
            // Overwrite webdriver on Navigator.prototype so navigator.hasOwnProperty('webdriver') === false
            try {
                delete Object.getPrototypeOf(navigator).webdriver;
            } catch (e) {}
            try {
                Object.defineProperty(Navigator.prototype, 'webdriver', {
                    get: () => undefined,
                    configurable: true,
                    enumerable: false,
                });
            } catch (e) {}

            // Realistic window.chrome object
            if (!window.chrome) {
                window.chrome = {};
            }
            if (!window.chrome.runtime) {
                window.chrome.runtime = {
                    connect: () => {},
                    sendMessage: () => {},
                    id: undefined,
                };
            }
            if (!window.chrome.loadTimes) {
                window.chrome.loadTimes = () => ({
                    requestTime: performance.now() / 1000,
                    startLoadTime: performance.now() / 1000,
                    commitLoadTime: performance.now() / 1000,
                    finishDocumentLoadTime: performance.now() / 1000,
                    firstPaintTime: performance.now() / 1000,
                    firstPaintAfterLoadTime: 0,
                    navigationType: 'Other',
                    wasFetchedViaSpdy: false,
                    wasNpnNegotiated: false,
                    npnNegotiatedProtocol: 'unknown',
                    wasAlternateProtocolAvailable: false,
                    connectionInfo: 'http/1.1',
                });
            }
            if (!window.chrome.csi) {
                window.chrome.csi = () => ({
                    startE: performance.now(),
                    onloadT: performance.now(),
                    pageT: performance.now(),
                    tran: 15,
                });
            }
            if (!window.chrome.app) {
                window.chrome.app = {
                    isInstalled: false,
                    InstallState: { DISABLED: "disabled", INSTALLED: "installed", NOT_INSTALLED: "not_installed" },
                    RunningState: { CANNOT_RUN: "cannot_run", READY_TO_RUN: "ready_to_run", RUNNING: "running" },
                    getIsInstalled: () => false,
                    getDetails: () => null,
                };
            }

            // Realistic languages
            try {
                Object.defineProperty(Navigator.prototype, 'languages', {
                    get: () => ['zh-CN', 'zh', 'en-US', 'en'],
                    configurable: true,
                });
            } catch (e) {}

            // Standard plugins
            try {
                const makePlugin = (name, filename, description) => ({
                    name,
                    filename,
                    description,
                    length: 1,
                    item: () => null,
                    namedItem: () => null,
                });
                const pluginsList = [
                    makePlugin('PDF Viewer', 'pdf-viewer.plugin', 'Portable Document Format'),
                    makePlugin('Chrome PDF Viewer', 'internal-pdf-viewer', 'Portable Document Format'),
                    makePlugin('Chromium PDF Viewer', 'internal-pdf-viewer', 'Portable Document Format'),
                ];
                Object.defineProperty(Navigator.prototype, 'plugins', {
                    get: () => pluginsList,
                    configurable: true,
                });
            } catch (e) {}
            """
            await self.context.add_init_script(STEALTH_INIT_SCRIPT)

            TRACKER_DOMAINS = (
                "sensorsdata.cn",
                "growingio.com",
                "hm.baidu.com",
                "google-analytics.com",
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

            # Sanitize profile cookies: purge dummy/corrupt values that trigger HTTP 500 on Xiaohongshu servers
            try:
                from backend.services.cookie_service import DUMMY_SUBSTRINGS
                loaded_cookies = await self.context.cookies()
                bad_cookies = [
                    c for c in loaded_cookies
                    if any(dummy in str(c.get("value", "")).lower() for dummy in DUMMY_SUBSTRINGS)
                ]
                if bad_cookies:
                    logger.warning("Purging %d dummy/corrupt cookies from Xiaohongshu browser profile...", len(bad_cookies))
                    clean_cookies = [
                        c for c in loaded_cookies
                        if not any(dummy in str(c.get("value", "")).lower() for dummy in DUMMY_SUBSTRINGS)
                    ]
                    await self.context.clear_cookies()
                    if clean_cookies:
                        await self.context.add_cookies(cast(Any, clean_cookies))
            except Exception as e:
                logger.debug("Profile cookie sanitization skipped: %s", e)

            # Restore saved session cookies if present (skipping any dummy values)
            auth_file = self.profile / "auth_state.json"
            if auth_file.exists():
                try:
                    from backend.services.cookie_service import persist_auth_cookies
                    auth_data = json.loads(auth_file.read_text("utf-8"))
                    cookies = auth_data.get("cookies", [])
                    if cookies:
                        persisted = persist_auth_cookies(cookies)
                        await self.context.add_cookies(cast(Any, persisted))
                except Exception as e:
                    logger.debug("Could not restore cookies from auth_state.json: %s", e)

            # Inject custom cookie from settings if configured (skip dummy/test strings)
            if settings.xhs_cookie:
                try:
                    from backend.services.cookie_service import (
                        is_dummy_cookie,
                        parse_cookie_str,
                        persist_auth_cookies,
                    )
                    if not is_dummy_cookie(settings.xhs_cookie):
                        tokens = parse_cookie_str(settings.xhs_cookie)
                        if tokens:
                            custom_cookies = [
                                {"name": k, "value": v, "domain": ".rednote.com", "path": "/"}
                                for k, v in tokens.items()
                            ] + [
                                {"name": k, "value": v, "domain": ".xiaohongshu.com", "path": "/"}
                                for k, v in tokens.items()
                            ]
                            persisted_custom = persist_auth_cookies(custom_cookies)
                            await self.context.add_cookies(cast(Any, persisted_custom))
                except Exception as e:
                    logger.debug("Could not inject custom settings cookie: %s", e)

            self.base_url = await self.detect_base_url()
            self.page = (
                self.context.pages[0] if self.context.pages else await self.context.new_page()
            )
            self.page.set_default_timeout(10000)
            target_home = initial_url or f"{self.base_url}/explore"

            # Domain fallback candidates
            nav_urls = [target_home]
            if "rednote.com" in target_home:
                nav_urls.append(target_home.replace("rednote.com", "xiaohongshu.com"))
            elif "xiaohongshu.com" in target_home:
                nav_urls.append(target_home.replace("xiaohongshu.com", "rednote.com"))

            nav_success = False
            last_nav_err = None
            for nav_url in nav_urls:
                try:
                    logger.info("Xiaohongshu navigating to: %s", nav_url)
                    resp = await self.page.goto(nav_url, wait_until="domcontentloaded", timeout=30000)
                    if resp is not None:
                        self.last_http_status = resp.status
                    nav_success = True
                    if "xiaohongshu.com" in nav_url:
                        self.base_url = "https://www.xiaohongshu.com"
                    elif "rednote.com" in nav_url:
                        self.base_url = "https://www.rednote.com"
                    break
                except Error as nav_err:
                    last_nav_err = nav_err
                    err_text = str(nav_err)
                    logger.warning("Xiaohongshu navigation failed for %s: %s", nav_url, err_text)
                    # If server returned HTTP 500 / ERR_HTTP_RESPONSE_CODE_FAILURE, stale/corrupt session cookies
                    # from past logins often crash Xiaohongshu's backend. Clear cookies and retry clean once.
                    if "ERR_HTTP_RESPONSE_CODE_FAILURE" in err_text or "500" in err_text:
                        logger.warning("Received HTTP response code failure on %s; clearing cookies and retrying clean...", nav_url)
                        try:
                            await self.context.clear_cookies()
                            retry_resp = await self.page.goto(nav_url, wait_until="domcontentloaded", timeout=30000)
                            if retry_resp is not None:
                                self.last_http_status = retry_resp.status
                            nav_success = True
                            if "xiaohongshu.com" in nav_url:
                                self.base_url = "https://www.xiaohongshu.com"
                            elif "rednote.com" in nav_url:
                                self.base_url = "https://www.rednote.com"
                            break
                        except Error as retry_err:
                            logger.error("Retry after cookie clear also failed for %s: %s", nav_url, retry_err)
                            last_nav_err = retry_err

            if not nav_success:
                raise last_nav_err or Error("Failed to navigate to Xiaohongshu endpoints")

            await self.page.wait_for_timeout(1000)
            await self.inspect(self.page)
            return self.page
        except Error as exc:
            logger.error("Xiaohongshu ensure_open error: %s", exc)
            navigation_failed = self.page is not None
            # A navigation failure can happen after Chrome already launched. Close the
            # partial context so it cannot keep the persistent profile locked.
            try:
                await self._close()
            except Error:
                pass
            self.state = "network_error"
            if navigation_failed:
                raise ProviderUnavailableError(
                    f"Chrome đã mở nhưng không tải được trang Xiaohongshu/Rednote ({exc}). "
                    "Hãy kiểm tra kết nối và mở lại nền tảng trong trình duyệt riêng; lỗi này không đồng nghĩa thiếu Chromium."
                ) from None
            raise ProviderUnavailableError(
                f"Không mở được trình duyệt Xiaohongshu ({exc}). Kiểm tra Chromium đã cài và đóng phiên dùng cùng profile."
            ) from None

    async def probe_live(self) -> tuple[bool, str]:
        """Probes live connectivity to Xiaohongshu using a dedicated page to avoid interrupting active searches."""
        probe_page: Page | None = None
        try:
            await self.ensure_open()
            if not self.context:
                return False, "Browser context unavailable"
            probe_page = await self.context.new_page()
            target_url = self.base_url or "https://www.xiaohongshu.com/explore"
            resp = await probe_page.goto(target_url, wait_until="domcontentloaded", timeout=20000)
            http_status = resp.status if resp is not None else None
            self.last_http_status = http_status
            state = await self.inspect(probe_page)

            is_http_ok = http_status is not None and 200 <= http_status < 400
            is_healthy = is_http_ok and state not in ("network_error", "closed", "restricted")
            status_desc = f"HTTP {http_status}: {state}" if http_status else state
            return is_healthy, status_desc
        except Exception as exc:
            logger.warning("Xiaohongshu live probe failed: %s", exc)
            self.last_http_status = None
            return False, f"error: {exc}"
        finally:
            if probe_page is not None:
                try:
                    if not probe_page.is_closed():
                        await probe_page.close()
                except Exception:
                    pass

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
            search_input = page.locator("input#search-input:visible").first
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
                    has_session_cookie = "web_session" in c_names or "id_token" in c_names
                except Exception:
                    pass

            user_nav = page.locator(
                ".side-bar a[href*='/user/profile/'], .side-bar :text('Me'), .side-bar :text('我'), .side-bar :text('Profile'), .side-bar :text('My Profile')"
            )
            user_avatar = page.locator(".side-bar img[src*='avatar'], .side-bar [class*='avatar']:visible, [class*='user-avatar']:visible")
            has_user_element = ((await user_nav.count()) > 0) or ((await user_avatar.count()) > 0) or has_session_cookie
            is_authenticated = bool(has_user_element or has_session_cookie)

            login_modal = page.locator(
                ".login-container:visible, .login-modal:visible, [class*='login-modal']:visible, .reds-modal.login-modal:visible"
            )
            login_modal_visible = (await login_modal.count()) > 0

            # Automatically dismiss login/promo modal if it has a close button or can be closed via Escape
            if login_modal_visible:
                try:
                    close_btn = page.locator(
                        ".icon-btn-wrapper.close-button:visible, .close-button:visible, .close:visible, [aria-label='关闭']:visible, [aria-label='Close']:visible, .dyn-modal-close:visible, svg.close-button:visible, .ant-modal-close:visible, .reds-modal [class*='close']:visible"
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

            # Check for verification / captcha container or iframes
            captcha_box = page.locator(
                "#captcha-verify-image, .captcha-verify-container:visible, .verify-bar-close:visible, [class*='captcha']:visible"
            )
            captcha_visible = (await captcha_box.count()) > 0
            if not captcha_visible and hasattr(page, "frames"):
                for f in page.frames:
                    if f != page.main_frame:
                        f_url = (f.url or "").lower()
                        if "verifycenter" in f_url or "nocaptcha" in f_url or "verify" in f_url or "captcha" in f_url:
                            captcha_visible = True
                            break

            if captcha_visible:
                self.state = "verification_required"

            if self.state in {"restricted", "verification_required"}:
                pass
            elif (
                "/login" in parts.path or "/website-login" in parts.path
                or "tourist_search" in parse_qs(parts.query).get("source", [])
            ):
                # A stale cookie is not evidence that the server accepted the session.
                self.state = "login_required"
            elif is_authenticated:
                self.state = "page_available"
            elif login_modal_visible:
                # If notes or video elements are present, the page is usable despite the modal
                note_items = await page.locator("section.note-item, div.note-item, .search-result-item, a[href*='/explore/']").count()
                if note_items > 0:
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

            note_links = await page.locator('a[href*="/explore/"]').count()
            video_count = await page.locator("video").count()

            self.diagnostics = {
                "host": parts.netloc,
                "path": parts.path,
                "query_param_names": query_param_names,
                "title": title[:200],
                "input_count": input_count,
                "search_input_value": search_val,
                "login_modal_visible": login_modal_visible,
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
                ".qrcode-mask:visible, .refresh-btn:visible, :text('点击刷新'):visible, :text('刷新'):visible"
            ).first
            if await refresh_btn.count() > 0:
                await refresh_btn.click(timeout=2000)
            else:
                target = f"{self.base_url}/login"
                await self.page.goto(target, wait_until="domcontentloaded", timeout=20000)
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
                    try:
                        from backend.services.cookie_service import persist_auth_cookies
                        raw_auth = json.loads(auth_file.read_text("utf-8"))
                        raw_auth["cookies"] = persist_auth_cookies(raw_auth.get("cookies", []))
                        auth_file.write_text(json.dumps(raw_auth, indent=2), encoding="utf-8")
                    except Exception:
                        pass
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
            if not getattr(self, "sticky_headful", False):
                self.hybrid_headless = getattr(self, "default_hybrid_headless", True)
                self.force_headful_session = False

    async def close(self) -> None:
        async with self.lock:
            await self._close()

