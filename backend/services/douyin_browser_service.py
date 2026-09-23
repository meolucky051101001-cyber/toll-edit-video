import asyncio
import json
import logging
from pathlib import Path
from typing import Any, cast
from urllib.parse import parse_qs, urlsplit

from playwright.async_api import BrowserContext, Error, Page, Playwright, async_playwright

from backend.core.config import ROOT, settings
from backend.providers.base import ProviderUnavailableError
from backend.providers.douyin.parser import classify_page

logger = logging.getLogger("douyin.browser")

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
        self.base_url = DOUYIN_HOME
        self.diagnostics: dict = {}
        self.allow_media = False
        self.last_http_status: int | None = None

    def set_allow_media(self, allow: bool) -> None:
        self.allow_media = allow

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

    async def ensure_open(self, headless_override: bool | None = None, initial_url: str | None = None) -> Page:
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

            # Unblock bds.snssdk.com / mon.snssdk.com / log.douyin.com so ByteDance Security SDK and CAPTCHA verify correctly
            TRACKER_DOMAINS = (
                "sensorsdata.cn",
                "growingio.com",
                "hm.baidu.com",
                "google-analytics.com",
            )

            async def block_resources(route):
                req = route.request
                url = req.url.lower()
                if any(domain in url for domain in TRACKER_DOMAINS):
                    await route.abort()
                    return
                # Only block media when allow_media is False (e.g. during search card scraping)
                if not getattr(self, "allow_media", False):
                    if (
                        req.resource_type in {"media", "font"}
                        or any(url.endswith(ext) or ext + "?" in url for ext in HEAVY_EXTENSIONS)
                    ):
                        await route.abort()
                        return
                elif req.resource_type == "font":
                    await route.abort()
                    return
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
                        await self.context.add_cookies(cast(Any, persisted))
                except Exception:
                    pass

            # Inject custom cookie from settings if configured (skip dummy/test strings)
            if settings.douyin_cookie:
                try:
                    from backend.services.cookie_service import (
                        is_dummy_cookie,
                        parse_cookie_str,
                        persist_auth_cookies,
                    )
                    if not is_dummy_cookie(settings.douyin_cookie):
                        tokens = parse_cookie_str(settings.douyin_cookie)
                        if tokens:
                            custom_cookies = [
                                {"name": k, "value": v, "domain": ".douyin.com", "path": "/"}
                                for k, v in tokens.items()
                            ]
                            persisted_custom = persist_auth_cookies(custom_cookies)
                            await self.context.add_cookies(cast(Any, persisted_custom))
                except Exception:
                    pass

            self.page = (
                self.context.pages[0] if self.context.pages else await self.context.new_page()
            )
            self.page.set_default_timeout(10000)
            nav_target = initial_url or self.base_url
            resp = await self.page.goto(nav_target, wait_until="domcontentloaded", timeout=30000)
            if resp is not None:
                self.last_http_status = resp.status
            if not initial_url:
                await self.page.wait_for_timeout(1500)
            await self.inspect(self.page)
            return self.page
        except Error as exc:
            logger.error("Douyin ensure_open error: %s", exc)
            try:
                await self._close()
            except Error:
                pass
            self.state = "network_error"
            raise ProviderUnavailableError(
                f"Không mở được trình duyệt Douyin ({exc}). Kiểm tra Chromium đã cài và đóng phiên dùng cùng profile."
            ) from None

    async def probe_live(self) -> tuple[bool, str]:
        """Probes live connectivity to Douyin using a dedicated page to avoid interrupting active searches."""
        probe_page: Page | None = None
        try:
            await self.ensure_open()
            if not self.context:
                return False, "Browser context unavailable"
            probe_page = await self.context.new_page()
            target_url = self.base_url or DOUYIN_HOME
            resp = await probe_page.goto(target_url, wait_until="domcontentloaded", timeout=20000)
            http_status = resp.status if resp is not None else None
            self.last_http_status = http_status
            state = await self.inspect(probe_page)

            is_http_ok = http_status is not None and 200 <= http_status < 400
            is_healthy = is_http_ok and state not in ("network_error", "closed", "restricted")
            status_desc = f"HTTP {http_status}: {state}" if http_status else state
            return is_healthy, status_desc
        except Exception as exc:
            logger.warning("Douyin live probe failed: %s", exc)
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

            # Check for dismissible login modal / mask
            login_modal = page.locator(
                "#login-panel:visible, .login-mask:visible, [class*='login-panel']:visible, [class*='login-modal']:visible, [class*='login-dialog']:visible"
            )
            login_modal_visible = (await login_modal.count()) > 0

            # Automatically dismiss guest login popup if present
            if login_modal_visible and not captcha_visible:
                try:
                    close_btn = page.locator(
                        ".login-panel-close:visible, .dy-account-close:visible, [data-e2e='close-icon']:visible, #login-panel [class*='close']:visible, .login-mask [class*='close']:visible, [aria-label='关闭']:visible, [aria-label='Close']:visible, .semi-modal-close:visible, [data-e2e='modal-close-button']:visible"
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

    async def screenshot_note_images(
        self,
        note_url: str,
        save_dir: Path,
        page: Page | None = None,
        max_slides: int | None = None,
    ) -> list[Path]:
        """Captures individual high-resolution screenshots for each slide of a Douyin image/note album."""
        save_dir.mkdir(parents=True, exist_ok=True)
        saved_paths: list[Path] = []
        created_page = False
        try:
            if page is None:
                if not self.context:
                    await self.ensure_open()
                if self.context:
                    page = await self.context.new_page()
                    created_page = True
                else:
                    page = self.page

            if not page or page.is_closed():
                return saved_paths

            try:
                await page.set_viewport_size({"width": 1280, "height": 900})
            except Exception:
                pass

            if created_page:
                await page.goto(note_url, wait_until="domcontentloaded", timeout=25000)
                await asyncio.sleep(1.5)

            # Detect indicator for total slides (e.g. "1/5", "2 / 6") if max_slides not specified
            if max_slides is None:
                max_slides = 20
                try:
                    indicator_text = await page.locator(
                        "[class*='indicator'], [class*='slider-tag'], [class*='pagination'], [class*='count']"
                    ).evaluate_all("els => els.map(e => e.innerText).join(' ')")
                    import re

                    m_ind = re.search(r"\d+\s*/\s*(\d+)", str(indicator_text))
                    if m_ind:
                        total_from_ind = int(m_ind.group(1))
                        if 1 <= total_from_ind <= 50:
                            max_slides = total_from_ind
                except Exception:
                    pass

            seen_sources: set[str] = set()
            consecutive_duplicate_steps = 0
            start_loop_time = asyncio.get_running_loop().time()
            max_duration_seconds = 30.0

            first_slide_src: str | None = None
            for _ in range(max_slides):
                if asyncio.get_running_loop().time() - start_loop_time > max_duration_seconds:
                    logger.debug("Reached timeout while capturing note slides from %s", note_url)
                    break

                # Query current active slide image and mark it in DOM
                slide_img = await page.evaluate(
                    """() => {
                        const container = document.querySelector('[data-e2e="note-detail"], .slider-video, [class*="slider"], [class*="carousel"]') || document.body;
                        container.querySelectorAll('img[data-current-active-slide="true"]').forEach(el => el.removeAttribute('data-current-active-slide'));
                        const imgs = Array.from(container.querySelectorAll('img')).filter(img => {
                            const r = img.getBoundingClientRect();
                            return r.width >= 150 && r.height >= 150 && r.bottom > 0 && r.top < window.innerHeight;
                        });
                        if (!imgs.length) return null;
                        imgs.sort((a, b) => {
                            const ra = a.getBoundingClientRect();
                            const rb = b.getBoundingClientRect();
                            return (rb.width * rb.height) - (ra.width * ra.height);
                        });
                        const best = imgs[0];
                        best.setAttribute('data-current-active-slide', 'true');
                        return best.currentSrc || best.src || best.getAttribute('data-src') || best.getAttribute('data-origin') || null;
                    }"""
                )

                if slide_img:
                    src_str = str(slide_img)
                    if first_slide_src is None:
                        first_slide_src = src_str
                    elif src_str == first_slide_src and len(saved_paths) >= 1:
                        logger.debug("Cycled back to initial slide (%s), stopping capture.", src_str)
                        break

                found_new = False
                if slide_img and str(slide_img) not in seen_sources:
                    seen_sources.add(str(slide_img))
                    out_file = save_dir / f"{len(saved_paths) + 1:03d}.png"
                    card = page.locator("img[data-current-active-slide='true']").first
                    try:
                        if await card.count() > 0:
                            await card.screenshot(path=str(out_file))
                        else:
                            fallback_card = page.locator(
                                "[data-e2e='note-detail'] img, .slider-video img, [class*='slider'] img"
                            ).first
                            if await fallback_card.count() > 0:
                                await fallback_card.screenshot(path=str(out_file))
                            else:
                                await page.screenshot(path=str(out_file))
                        if out_file.exists() and out_file.stat().st_size > 0:
                            saved_paths.append(out_file)
                            found_new = True
                    except Exception:
                        pass

                if found_new:
                    consecutive_duplicate_steps = 0
                else:
                    consecutive_duplicate_steps += 1
                    if consecutive_duplicate_steps >= 2 and len(saved_paths) >= 1:
                        break

                # Advance to next slide via button or keyboard
                next_btn = page.locator(
                    "[class*='arrow-right'], [class*='slider-arrow-right'], [class*='switch-right'], [class*='btn-next'], [data-e2e*='next'], button[aria-label*='下一']"
                ).first
                clicked = False
                try:
                    if await next_btn.count() > 0 and await next_btn.is_visible():
                        await next_btn.click(timeout=800)
                        clicked = True
                except Exception:
                    pass

                if not clicked:
                    try:
                        await page.keyboard.press("ArrowRight")
                    except Exception:
                        pass

                await asyncio.sleep(0.4)

            # Fallback if no large images were screenshot
            if not saved_paths:
                fallback_file = save_dir / "001.png"
                card = page.locator("[data-e2e='note-detail'], .note-detail, .slider-video, [class*='slider']").first
                if await card.count() > 0:
                    await card.screenshot(path=str(fallback_file))
                else:
                    await page.screenshot(path=str(fallback_file))
                saved_paths.append(fallback_file)

        except Exception as err:
            logger.warning("Failed screenshotting note images from %s: %s", note_url, err)
        finally:
            if created_page and page and not page.is_closed():
                try:
                    await page.close()
                except Exception:
                    pass
        return saved_paths
