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
from backend.services.browser_session import (
    load_headful_preference,
    platform_navigation_url,
    restore_missing_cookies,
    save_headful_preference,
    save_session_checkpoint,
    visible_challenge_evidence,
)

logger = logging.getLogger("xiaohongshu.browser")

HOME = "https://www.xiaohongshu.com/explore"
MESSAGES = {
    "authenticated": "Đã đăng nhập Xiaohongshu. Có thể bắt đầu tìm kiếm.",
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
        self.sticky_headful = load_headful_preference(self.profile)
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
        save_headful_preference(self.profile)
        if not getattr(self, "is_currently_headless", False):
            try:
                return self.get_active_page()
            except ProviderUnavailableError:
                return await self.ensure_open(headless_override=False)
        try:
            active = self.get_active_page()
            current_url = platform_navigation_url(active.url, ("xiaohongshu.com", "rednote.com"))
        except ProviderUnavailableError:
            current_url = None
        await self._close()
        # Navigate directly to the blocked route, never via the home feed.
        page = await self.ensure_open(headless_override=False, initial_url=current_url)
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
            "message": MESSAGES.get(self.state, "Trạng thái trình duyệt Xiaohongshu không xác định."),
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

    def session_navigation_url(self, initial_url: str | None) -> str:
        if initial_url:
            parsed = urlsplit(initial_url)
            if parsed.hostname in {"rednote.com", "www.rednote.com", "xiaohongshu.com", "www.xiaohongshu.com"}:
                return f"{self.base_url}{parsed.path}" + (f"?{parsed.query}" if parsed.query else "")
        return f"{self.base_url}/explore"

    async def navigate_preserving_session(self, target_url: str) -> None:
        assert self.page is not None
        urls = [target_url]
        if "rednote.com" in urlsplit(target_url).netloc:
            urls.append(target_url.replace("rednote.com", "xiaohongshu.com", 1))
        else:
            urls.append(target_url.replace("xiaohongshu.com", "rednote.com", 1))
        last_error = None
        for attempt, url in enumerate(urls):
            if attempt and self.context:
                # A failed navigation can still be redirecting in the background.
                # Use a fresh tab for the alternate domain, within the same session.
                failed_page = self.page
                self.page = await self.context.new_page()
                self.page.set_default_timeout(10000)
                try:
                    await failed_page.close()
                except Error:
                    pass
            try:
                response = await self.page.goto(url, wait_until="domcontentloaded", timeout=30000)
                self.last_http_status = response.status if response else None
                if response and response.status >= 400:
                    raise Error(f"Xiaohongshu returned HTTP {response.status}")
                parsed = urlsplit(self.page.url)
                if parsed.scheme != "https" or parsed.hostname not in {"rednote.com", "www.rednote.com", "xiaohongshu.com", "www.xiaohongshu.com"}:
                    raise Error("Xiaohongshu navigation did not reach a trusted platform page")
                self.base_url = f"{parsed.scheme}://{parsed.netloc}"
                return
            except Error as error:
                # HTTP failures are not evidence that authentication cookies are invalid.
                # Keep the user's session and try the alternate public domain once.
                last_error = error
                logger.warning("Xiaohongshu navigation failed: %s", type(error).__name__)
        raise last_error or Error("Failed to navigate to Xiaohongshu endpoints")

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
            if self.sticky_headful:
                effective_headless = False
            elif headless_override is not None:
                effective_headless = headless_override
            else:
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
                    "--disable-blink-features=AutomationControlled",
                    "--disable-dev-shm-usage",
                    "--no-first-run",
                    "--no-default-browser-check",
                    "--window-size=1280,900",
                ],
            )

            # Use the installed browser's native UA, plugins and security isolation.


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
                    auth_data = json.loads(auth_file.read_text("utf-8"))
                    cookies = auth_data.get("cookies", [])
                    if cookies:
                        await restore_missing_cookies(self.context, cookies)
                except Exception as e:
                    logger.debug("Could not restore cookies from auth_state.json: %s", e)

            # Inject custom cookie from settings if configured (skip dummy/test strings)
            if settings.xhs_cookie:
                try:
                    from backend.services.cookie_service import (
                        is_dummy_cookie,
                        parse_cookie_str,
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
                            await restore_missing_cookies(self.context, custom_cookies)
                except Exception as e:
                    logger.debug("Could not inject custom settings cookie: %s", e)

            self.base_url = await self.detect_base_url()
            self.page = (
                self.context.pages[0] if self.context.pages else await self.context.new_page()
            )
            self.page.set_default_timeout(10000)
            # Preserve the path/query while choosing the domain with the saved session.
            target_home = self.session_navigation_url(initial_url)
            await self.navigate_preserving_session(target_home)

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
        if self.lock.locked():
            return False, "browser_busy"
        async with self.lock:
            return await self._probe_live_unlocked()

    async def _probe_live_unlocked(self) -> tuple[bool, str]:
        """Probes live connectivity to Xiaohongshu using a dedicated page to avoid interrupting active searches."""
        probe_page: Page | None = None
        active_state: str | None = None
        active_diagnostics: dict = {}
        try:
            await self.ensure_open()
            active_state = self.state
            active_diagnostics = dict(self.diagnostics)
            if not self.context:
                return False, "Browser context unavailable"
            probe_page = await self.context.new_page()
            target_url = self.base_url or "https://www.xiaohongshu.com/explore"
            resp = await probe_page.goto(target_url, wait_until="domcontentloaded", timeout=30000)
            http_status = resp.status if resp is not None else None
            self.last_http_status = http_status
            state = await self.inspect(probe_page)
            if active_state in {"login_required", "verification_required", "restricted"}:
                state = active_state

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
            if active_state is not None:
                self.state = active_state
                self.diagnostics = active_diagnostics

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

            # Check for explicit visible login buttons / links (presence means user is NOT logged in)
            login_buttons = page.locator(
                "button:has-text('登录'):visible, a:has-text('登录'):visible, .login-btn:visible, [class*='login-btn']:visible, .side-bar :text('登录'):visible, [aria-label*='登录']:visible, :text('立即登录'):visible, button:has-text('Log In'):visible, button:has-text('Sign In'):visible"
            )
            has_login_button = (await login_buttons.count()) > 0

            # Account-specific identity elements (do NOT use generic sidebar tabs like '我' or 'Me' which appear for guests,
            # and do NOT match note author links in explore feed)
            user_profile_link = page.locator(
                ".side-bar a[href*='/user/profile/']:visible, .user-nav a[href*='/user/profile/']:visible"
            )
            user_avatar = page.locator(
                ".side-bar img[src*='avatar']:visible, .side-bar img[src*='sns-webpic']:visible, .side-bar [class*='avatar']:visible, .user-nav img[src*='avatar']:visible, .user-nav [class*='avatar']:visible"
            )
            has_user_element = ((await user_profile_link.count()) > 0) or ((await user_avatar.count()) > 0)

            # Check client-side Vue reactive user state if available (from window.__INITIAL_STATE__)
            js_logged_in: bool | None = None
            try:
                js_logged_in = await page.evaluate("""() => {
                    try {
                        const u = window.__INITIAL_STATE__ && window.__INITIAL_STATE__.user;
                        if (!u || typeof u !== 'object') return null;
                        const raw = u.loggedIn;
                        if (typeof raw === 'boolean') return raw;
                        if (raw && typeof raw === 'object') {
                            if ('_value' in raw && typeof raw._value === 'boolean') return raw._value;
                            if ('value' in raw && typeof raw.value === 'boolean') return raw.value;
                        }
                        return null;
                    } catch (e) {
                        return null;
                    }
                }""")
            except Exception:
                js_logged_in = None

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

            # Re-check login button visibility after modal close attempt
            has_login_button = (await login_buttons.count()) > 0

            is_login_route = (
                "/login" in parts.path or "/website-login" in parts.path
                or "tourist_search" in parse_qs(parts.query).get("source", [])
            )

            # Strict authentication: must have session cookie, user element, no modal, NO login buttons, no login route, and js_logged_in != False
            is_authenticated = bool(
                has_session_cookie
                and has_user_element
                and not login_modal_visible
                and not has_login_button
                and not is_login_route
                and js_logged_in is not False
            )

            captcha_evidence = await visible_challenge_evidence(page)
            captcha_visible = bool(captcha_evidence) or self.state == "verification_required"

            if captcha_visible:
                self.state = "verification_required"

            if self.state in {"restricted", "verification_required"}:
                pass
            elif is_login_route:
                # A stale cookie is not evidence that the server accepted the session.
                self.state = "login_required"
            elif login_modal_visible:
                self.state = "login_required"
            elif is_authenticated:
                self.state = "authenticated"
            else:
                self.state = "page_available"

            # Persist session state whenever context exists with active cookies
            if self.context:
                try:
                    c_list = await self.context.cookies()
                    if c_list:
                        await save_session_checkpoint(self.context, self.profile)
                except Exception:
                    pass

            note_links = await page.locator(
                'a[href*="/explore/"], a[href*="/search_result/"], a[href*="/discovery/item/"]'
            ).count()
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
                "captcha_detection_reason": captcha_evidence or ("visible_challenge_text" if captcha_visible else None),
                "has_login_button": has_login_button,
                "is_authenticated": is_authenticated,
                "result_links_count": note_links,
                "video_elements_count": video_count,
            }
        except Exception as exc:
            logger.debug("Exception in inspect: %s", exc, exc_info=True)
            self.state = "network_error"
        return self.state

    async def open(self, headful: bool = True) -> dict:
        if self.lock.locked():
            return self.snapshot()
        async with self.lock:
            if headful:
                page = await self.elevate_to_headful()
            else:
                page = await self.ensure_open()
                page = self.get_active_page()
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
                    await save_session_checkpoint(self.context, self.profile)
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

