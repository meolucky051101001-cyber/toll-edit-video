import os
import tempfile
from pathlib import Path
from typing import Literal

from dotenv import set_key
from fastapi import APIRouter, Request
from pydantic import BaseModel, ConfigDict, Field, SecretStr

from backend.core.config import ROOT, settings
from backend.providers.ai.gemini import FREE_TIER_MODELS

router = APIRouter(prefix="/api/settings", tags=["settings"])


@router.get("")
def read_settings():
    return {
        "use_mock_provider": settings.use_mock_provider,
        "default_result_limit": settings.default_result_limit,
        "max_queries": settings.max_queries,
        "search_timeout": settings.search_timeout,
        "database_location": str(ROOT / "data/app.db")
        if settings.database_url.startswith("sqlite:///./data/app.db")
        else "DATABASE_URL trong .env",
        "ai_available": settings.ai_provider == "gemini"
        and bool(settings.ai_api_key.get_secret_value())
        and settings.ai_free_tier_confirmed
        and settings.ai_model in FREE_TIER_MODELS,
        "ai_provider": settings.ai_provider,
        "ai_model": settings.ai_model,
        "ai_key_configured": bool(settings.ai_api_key.get_secret_value()),
        "ai_free_tier_confirmed": settings.ai_free_tier_confirmed,
        "ai_min_interval": settings.ai_min_interval,
        "browser_available": True,
    }


@router.post("/ai/test")
async def test_ai(request: Request):
    outcome = await request.app.state.ai.expand("unbox đồ cute")
    return {
        "ok": outcome.source == "gemini",
        "cached": outcome.cached,
        "message": "Kết nối Gemini thành công; JSON đã được kiểm tra."
        if outcome.source == "gemini"
        else outcome.warning,
    }


class AISettingsPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    ai_provider: Literal["gemini", ""]
    ai_model: Literal["gemini-3.1-flash-lite", "gemini-3.8-flash"]
    ai_api_key: SecretStr | None = Field(default=None, max_length=500)
    ai_free_tier_confirmed: bool


def _save_env_values(values: dict[str, str]) -> None:
    target = ROOT / ".env"
    fd, name = tempfile.mkstemp(prefix=".env.", dir=ROOT)
    staging = Path(name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as file:
            file.write(target.read_text(encoding="utf-8") if target.exists() else "")
        for field, value in values.items():
            set_key(str(staging), field, value)
        os.replace(staging, target)
    finally:
        staging.unlink(missing_ok=True)


@router.patch("")
async def update_settings(payload: AISettingsPatch, request: Request):
    async with request.app.state.ai.lock:
        key = payload.ai_api_key.get_secret_value().strip() if payload.ai_api_key else ""
        values = {
            "AI_PROVIDER": payload.ai_provider,
            "AI_MODEL": payload.ai_model,
            "AI_FREE_TIER_CONFIRMED": str(payload.ai_free_tier_confirmed).lower(),
        }
        if key:
            values["AI_API_KEY"] = key
        _save_env_values(values)
        settings.ai_provider = payload.ai_provider
        settings.ai_model = payload.ai_model
        settings.ai_free_tier_confirmed = payload.ai_free_tier_confirmed
        if key:
            settings.ai_api_key = SecretStr(key)
        request.app.state.ai.clear_cache()
    return read_settings()


class CookieSettingsPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    xhs_cookie: str | None = Field(default=None, max_length=15000)
    douyin_cookie: str | None = Field(default=None, max_length=15000)


class CookieSyncRequest(BaseModel):
    platform: Literal["xiaohongshu", "douyin", "all"] = "all"


async def sync_douyin_upstream_cookie(cookie: str, base_url: str) -> bool:
    if not cookie or not base_url:
        return False
    clean_base = base_url.rstrip("/")
    endpoint = (
        f"{clean_base}/hybrid/update_cookie"
        if clean_base.endswith("/api")
        else f"{clean_base}/api/hybrid/update_cookie"
    )
    payload = {"service": "douyin", "cookie": cookie}
    try:
        import httpx

        async with httpx.AsyncClient(timeout=4.0) as client:
            res = await client.post(endpoint, json=payload)
            return res.status_code == 200
    except Exception:
        return False


@router.get("/cookies")
async def get_cookie_settings():
    from backend.services.cookie_service import get_browser_session_cookie, inspect_cookie_tokens, is_dummy_cookie

    xhs_session = get_browser_session_cookie("xiaohongshu")
    douyin_session = get_browser_session_cookie("douyin")

    # Evaluate active cookie (from settings or fallback to browser session)
    active_xhs = settings.xhs_cookie if (settings.xhs_cookie and not is_dummy_cookie(settings.xhs_cookie)) else xhs_session
    active_douyin = settings.douyin_cookie if (settings.douyin_cookie and not is_dummy_cookie(settings.douyin_cookie)) else douyin_session

    xhs_status = inspect_cookie_tokens(active_xhs, "xiaohongshu")
    xhs_status["is_custom"] = bool(settings.xhs_cookie and not is_dummy_cookie(settings.xhs_cookie))
    xhs_status["has_browser_session"] = bool(xhs_session)

    douyin_status = inspect_cookie_tokens(active_douyin, "douyin")
    douyin_status["is_custom"] = bool(settings.douyin_cookie and not is_dummy_cookie(settings.douyin_cookie))
    douyin_status["has_browser_session"] = bool(douyin_session)

    return {
        "xiaohongshu": xhs_status,
        "douyin": douyin_status,
    }


@router.post("/cookies")
async def save_cookie_settings(payload: CookieSettingsPatch):
    values: dict[str, str] = {}
    if payload.xhs_cookie is not None:
        clean_xhs = payload.xhs_cookie.strip()
        values["XHS_COOKIE"] = clean_xhs
        settings.xhs_cookie = clean_xhs

    if payload.douyin_cookie is not None:
        clean_dy = payload.douyin_cookie.strip()
        values["DOUYIN_COOKIE"] = clean_dy
        settings.douyin_cookie = clean_dy
        # Async push to Douyin upstream if running
        if clean_dy:
            await sync_douyin_upstream_cookie(clean_dy, settings.douyin_downloader_url)

    if values:
        _save_env_values(values)

    return await get_cookie_settings()


@router.post("/cookies/sync-browser")
async def sync_browser_cookies(payload: CookieSyncRequest, request: Request):
    from backend.services.cookie_service import get_browser_session_cookie

    synced: list[str] = []
    values: dict[str, str] = {}

    if payload.platform in {"xiaohongshu", "all"}:
        xhs_cookie = ""
        # Try active playwright context first
        try:
            browser = request.app.state.browser
            if browser.context:
                cookies = await browser.context.cookies()
                if cookies:
                    xhs_cookie = "; ".join(f"{c['name']}={c['value']}" for c in cookies)
        except Exception:
            pass
        if not xhs_cookie:
            xhs_cookie = get_browser_session_cookie("xiaohongshu")

        if xhs_cookie:
            values["XHS_COOKIE"] = xhs_cookie
            settings.xhs_cookie = xhs_cookie
            synced.append("xiaohongshu")

    if payload.platform in {"douyin", "all"}:
        douyin_cookie = ""
        try:
            dy_browser = request.app.state.douyin_browser
            if dy_browser.context:
                cookies = await dy_browser.context.cookies()
                if cookies:
                    douyin_cookie = "; ".join(f"{c['name']}={c['value']}" for c in cookies)
        except Exception:
            pass
        if not douyin_cookie:
            douyin_cookie = get_browser_session_cookie("douyin")

        if douyin_cookie:
            values["DOUYIN_COOKIE"] = douyin_cookie
            settings.douyin_cookie = douyin_cookie
            synced.append("douyin")
            await sync_douyin_upstream_cookie(douyin_cookie, settings.douyin_downloader_url)

    if values:
        _save_env_values(values)

    return {
        "ok": len(synced) > 0,
        "synced_platforms": synced,
        "message": f"Đã đồng bộ Cookie từ trình duyệt cho: {', '.join(synced)}"
        if synced
        else "Không tìm thấy phiên đăng nhập trên trình duyệt để đồng bộ.",
        "cookies": await get_cookie_settings(),
    }

