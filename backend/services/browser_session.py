"""Local session checkpoints and rendered challenge detection shared by providers."""
from __future__ import annotations

import json
import os
import re
import shutil
import tempfile
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any, cast
from urllib.parse import urlsplit

from playwright.async_api import BrowserContext, Error, Page

from backend.services.cookie_service import persist_auth_cookies

#: Cookie names that prove a real login session exists. A checkpoint holding
#: only anonymous/tracking cookies must not be treated as an authenticated
#: session when deciding whether a headless search can run.
SESSION_COOKIE_NAMES = frozenset({
    "sessionid", "sessionid_ss", "sid_guard",  # Douyin
    "web_session", "id_token",                 # Xiaohongshu
})


def checkpoint_has_session_cookie(state: Mapping[str, Any]) -> bool:
    """True when the checkpoint carries a genuine login session cookie."""
    summary = state.get("auth_summary")
    if isinstance(summary, dict):
        return summary.get("has_session_cookie") is True
    names = {c.get("name") for c in state.get("cookies", []) if isinstance(c, dict)}
    return bool(names & SESSION_COOKIE_NAMES)


def load_headful_preference(profile: Path) -> bool:
    try:
        saved = json.loads((profile / "browser_preferences.json").read_text("utf-8"))
        return isinstance(saved, dict) and saved.get("prefer_headful") is True
    except (OSError, ValueError):
        return False


def save_headful_preference(profile: Path) -> None:
    """Remember the user-opened browser mode without storing credentials."""
    profile.mkdir(parents=True, exist_ok=True)
    target = profile / "browser_preferences.json"
    temporary: str | None = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=profile,
                                         prefix="browser-mode-", suffix=".tmp", delete=False) as out:
            temporary = out.name
            json.dump({"prefer_headful": True}, out)
        os.replace(temporary, target)
    finally:
        if temporary and os.path.exists(temporary):
            os.unlink(temporary)


STALE_LOCK_NAMES = frozenset({
    "SingletonLock",
    "SingletonSocket",
    "SingletonCookie",
    "DevToolsActivePort",
})


def cleanup_stale_profile_locks(profile: Path) -> list[str]:
    """Remove Chromium single-instance locks and pending-delete leftovers.

    Call before launch_persistent_context: a previous crash leaves Singleton*
    locks and *.CHROME_DELETE directories behind, and the next launch then
    dies with Access denied while Chrome tries to rename them. Only lock
    artifacts at the profile root are removed; Default/, Local State,
    cookies and auth_state.json are never touched. Returns removed names.
    """
    removed: list[str] = []
    try:
        entries = list(profile.iterdir())
    except OSError:
        return removed
    for entry in entries:
        if entry.name not in STALE_LOCK_NAMES and not entry.name.endswith(".CHROME_DELETE"):
            continue
        try:
            if entry.is_dir() and not entry.is_symlink():
                shutil.rmtree(entry, ignore_errors=True)
            else:
                entry.unlink(missing_ok=True)
        except OSError:
            continue
        if not os.path.lexists(entry):
            removed.append(entry.name)
    return removed


def cookie_key(cookie: Mapping[str, Any]) -> tuple[str, str, str]:
    return (cookie.get("name", ""), cookie.get("domain", ""), cookie.get("path", "/"))


async def restore_missing_cookies(context: BrowserContext, cookies: list[dict]) -> None:
    """The persistent profile wins over older snapshots or configured cookies."""
    existing = {cookie_key(c) for c in await context.cookies()}
    missing = [c for c in persist_auth_cookies(cookies) if cookie_key(c) not in existing]
    if missing:
        await context.add_cookies(cast(Any, missing))


async def save_session_checkpoint(context: BrowserContext, profile: Path) -> None:
    """Replace a complete checkpoint atomically; never write a partial JSON file."""
    raw_state = await context.storage_state()
    if not isinstance(raw_state, dict) or not isinstance(raw_state.get("cookies"), list):
        return
    state: dict[str, Any] = dict(raw_state)
    state["cookies"] = persist_auth_cookies(state["cookies"])
    cookie_names = {c.get("name") for c in state["cookies"] if isinstance(c, dict)}
    state["auth_summary"] = {
        "has_session_cookie": bool(cookie_names & SESSION_COOKIE_NAMES),
        "saved_at": time.time(),
    }
    profile.mkdir(parents=True, exist_ok=True)
    temporary: str | None = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=profile,
                                         prefix="auth-", suffix=".tmp", delete=False) as out:
            temporary = out.name
            json.dump(state, out, ensure_ascii=False)
            out.flush()
            os.fsync(out.fileno())
        os.replace(temporary, profile / "auth_state.json")
    finally:
        if temporary and os.path.exists(temporary):
            os.unlink(temporary)


def platform_navigation_url(url: str | None, domains: tuple[str, ...]) -> str | None:
    if not url:
        return None
    parsed = urlsplit(url)
    host = (parsed.hostname or "").lower()
    if parsed.scheme == "https" and any(host == d or host.endswith("." + d) for d in domains):
        return url
    return None


async def visible_challenge_evidence(page: Page) -> str | None:
    """Dormant or hidden security SDK frames are not a challenge."""
    selectors = (
        "#captcha-verify-image", ".captcha-verify-container", ".captcha-verify-panel",
        ".captcha_verify_container", "[data-e2e='captcha']", "[data-testid*='captcha']",
        ".secsdk-captcha-drag-icon",
        ".nc_scale", ".nc-container", ".btn_slide", ".verify-bar-area",
        ".verify-move-block", ".geetest_holder", ".geetest_slider",
        ".captcha-modal", "[class*='captcha-modal']", "[class*='captcha-container']",
        "[class*='verify-container']", "[class*='security-verification']",
        "[class*='verify-panel']", ".reds-captcha",
    )
    for selector in selectors:
        try:
            for candidate in await page.locator(selector).all():
                if not await candidate.is_visible():
                    continue
                box = await candidate.bounding_box()
                if not box or box["width"] < 40 or box["height"] < 25:
                    continue
                opacity = await candidate.evaluate(
                    "element => Number.parseFloat(getComputedStyle(element).opacity || '1')"
                )
                if float(opacity) > 0.05:
                    return f"visible_challenge_element:{selector}"
        except (Error, TypeError, ValueError):
            continue
    for frame in getattr(page, "frames", []):
        try:
            if not re.search(r"verifycenter|nocaptcha|captcha", (frame.url or "").lower()):
                continue
            element = await frame.frame_element()
            if not await element.is_visible():
                continue
            box = await element.bounding_box()
            if not box or box["width"] < 100 or box["height"] < 60:
                continue
            opacity = await element.evaluate(
                "element => Number.parseFloat(getComputedStyle(element).opacity || '1')"
            )
            if float(opacity) > 0.05:
                return "visible_captcha_iframe"
        except (Error, TypeError, ValueError, AttributeError):
            continue
    return None
