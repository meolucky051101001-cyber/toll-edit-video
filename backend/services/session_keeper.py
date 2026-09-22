"""Session Keep-Alive Service.

Periodically verifies and refreshes saved browser session cookies
to prevent session expiry and minimize CAPTCHA challenges.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from pathlib import Path
from typing import Any

from backend.core.config import ROOT

logger = logging.getLogger("session.keeper")


class SessionKeepAliveService:
    def __init__(
        self,
        douyin_profile: Path | None = None,
        xhs_profile: Path | None = None,
        heartbeat_interval_seconds: float = 21600.0,  # 6 hours default
        douyin_browser: Any | None = None,
        xhs_browser: Any | None = None,
    ):
        self.douyin_profile = douyin_profile or (ROOT / "data" / "browser" / "douyin")
        self.xhs_profile = xhs_profile or (ROOT / "data" / "browser" / "xiaohongshu")
        self.heartbeat_interval = heartbeat_interval_seconds
        self.browsers: dict[str, Any] = {
            "douyin": douyin_browser,
            "xiaohongshu": xhs_browser,
        }
        self.last_heartbeat: dict[str, float] = {}
        self.status: dict[str, dict[str, Any]] = {
            "douyin": {"active": False, "cookie_count": 0, "last_ping": None, "state": "idle"},
            "xiaohongshu": {"active": False, "cookie_count": 0, "last_ping": None, "state": "idle"},
        }
        self._task: asyncio.Task[None] | None = None
        self._running = False

    def register_browser(self, platform: str, browser_service: Any) -> None:
        self.browsers[platform] = browser_service

    def inspect_saved_cookies(self, platform: str) -> list[dict[str, Any]]:
        profile = self.douyin_profile if platform == "douyin" else self.xhs_profile
        auth_file = profile / "auth_state.json"
        if not auth_file.is_file():
            return []
        try:
            data = json.loads(auth_file.read_text(encoding="utf-8"))
            cookies = data.get("cookies", [])
            if isinstance(cookies, list):
                return cookies
        except Exception as exc:
            logger.warning("Failed reading auth_state for %s: %s", platform, exc)
        return []

    def get_status(self) -> dict[str, Any]:
        now = time.time()
        res: dict[str, Any] = {}
        for p in ("douyin", "xiaohongshu"):
            cookies = self.inspect_saved_cookies(p)
            has_auth = any(
                c.get("name") in ("sessionid", "passport_csrf_token", "web_session", "id_token")
                for c in cookies
            )
            last_p = self.last_heartbeat.get(p)
            res[p] = {
                "saved_cookies": len(cookies),
                "has_auth_session": has_auth,
                "last_heartbeat_s_ago": round(now - last_p, 1) if last_p else None,
                "status": "healthy" if has_auth else "unauthenticated",
            }
        return res

    async def ping_platform(self, platform: str, browser_service: Any = None) -> bool:
        """Sends a lightweight ping to keep cookies fresh if browser service is available and not busy."""
        if not browser_service:
            self.last_heartbeat[platform] = time.time()
            return True

        if hasattr(browser_service, "lock") and browser_service.lock.locked():
            logger.debug("Skipping session ping for %s: browser is currently busy", platform)
            return False

        try:
            async with browser_service.lock:
                page = getattr(browser_service, "page", None)
                if page and not page.is_closed():
                    target_url = "https://www.douyin.com/" if platform == "douyin" else "https://www.xiaohongshu.com/explore"
                    await page.evaluate(f"() => fetch('{target_url}', {{ method: 'HEAD' }}).catch(() => {{}})")
                    self.last_heartbeat[platform] = time.time()
                    logger.info("Session heartbeat sent for %s", platform)
                    return True
        except Exception as err:
            logger.warning("Session heartbeat error for %s: %s", platform, err)
        return False

    async def _heartbeat_loop(self) -> None:
        while self._running:
            try:
                for platform, browser in list(self.browsers.items()):
                    if not self._running:
                        break
                    cookies = self.inspect_saved_cookies(platform)
                    if not cookies:
                        continue

                    if browser and hasattr(browser, "lock") and browser.lock.locked():
                        logger.debug("Browser for %s is busy, skipping heartbeat this cycle", platform)
                        continue

                    try:
                        await self.ping_platform(platform, browser)
                    except Exception as err:
                        logger.warning("Session heartbeat error for %s: %s", platform, err)
            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.error("Error in session keeper heartbeat loop: %s", exc)

            sleep_remaining = self.heartbeat_interval
            while self._running and sleep_remaining > 0:
                step = min(sleep_remaining, 1.0)
                await asyncio.sleep(step)
                sleep_remaining -= step

    async def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(self._heartbeat_loop())
        logger.info("SessionKeepAliveService started with %s interval", self.heartbeat_interval)

    async def stop(self) -> None:
        self._running = False
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        self._task = None
        logger.info("SessionKeepAliveService stopped")
