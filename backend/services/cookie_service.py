import json
import logging
from pathlib import Path
from typing import Any

from backend.core.config import ROOT

logger = logging.getLogger(__name__)

XHS_ESSENTIAL_TOKENS = ["a1", "web_session", "webId"]
DOUYIN_ESSENTIAL_TOKENS = ["ttwid", "s_v_web_id", "sessionid"]
DUMMY_SUBSTRINGS = ["test_val", "sess_val", "wid_val", "test_sess", "test_ttwid", "mock_"]


def is_dummy_cookie(cookie_str: str | None) -> bool:
    """Detect if a cookie string contains synthetic dummy/placeholder values."""
    if not cookie_str:
        return False
    lower = str(cookie_str).lower()
    return any(dummy in lower for dummy in DUMMY_SUBSTRINGS)


def persist_auth_cookies(cookies: list[dict]) -> list[dict]:
    """Keep native cookie lifetimes; disk snapshots preserve session cookies.

    Extending an expiry locally cannot extend a session on the platform server.
    Expired cookies must not be resurrected during a browser restart.
    """
    import time
    now = time.time()
    updated = []
    for c in cookies:
        if not isinstance(c, dict):
            continue
        item = dict(c)
        val = str(item.get("value", "")).lower()
        if any(dummy in val for dummy in DUMMY_SUBSTRINGS):
            continue
        expires = item.get("expires", -1)
        if not isinstance(expires, (int, float)):
            continue
        if 0 < expires <= now:
            continue
        updated.append(item)
    return updated


def parse_cookie_str(cookie_str: str) -> dict[str, str]:
    """Parse cookie string into a key-value dictionary."""
    if not cookie_str or not cookie_str.strip():
        return {}
    res: dict[str, str] = {}
    parts = cookie_str.split(";")
    for part in parts:
        if "=" in part:
            k, v = part.strip().split("=", 1)
            k = k.strip()
            v = v.strip()
            if k:
                res[k] = v
    return res


def cookie_dict_to_str(cookies: dict[str, str]) -> str:
    """Format cookie dictionary to standard HTTP header string."""
    return "; ".join(f"{k}={v}" for k, v in cookies.items() if k and v)


def mask_cookie_preview(cookie_str: str) -> str:
    """Provide a safe preview of cookie tokens without leaking secrets."""
    tokens = parse_cookie_str(cookie_str)
    if not tokens:
        return "Chưa cấu hình"
    items = []
    for k, v in list(tokens.items())[:3]:
        masked_val = f"{v[:4]}...{v[-3:]}" if len(v) > 8 else "***"
        items.append(f"{k}={masked_val}")
    preview = "; ".join(items)
    if len(tokens) > 3:
        preview += f" (+{len(tokens) - 3} cookies)"
    return preview


def inspect_cookie_tokens(cookie_str: str, platform: str) -> dict[str, Any]:
    """Inspect cookie string and report health, count, and key tokens."""
    tokens = parse_cookie_str(cookie_str)
    has_cookie = bool(tokens)
    keys = set(tokens.keys())

    if platform == "xiaohongshu":
        essential = XHS_ESSENTIAL_TOKENS
        has_session = "web_session" in keys
        has_device = "a1" in keys or "webId" in keys
    else:  # douyin
        essential = DOUYIN_ESSENTIAL_TOKENS
        has_session = "sessionid" in keys or "passport_csrf_token" in keys
        has_device = "ttwid" in keys or "s_v_web_id" in keys

    detected_essential = [t for t in essential if t in keys]
    if not has_cookie:
        grade = "missing"
        status_text = "Chưa có Cookie. Video có thể bị giới hạn độ phân giải hoặc gặp chặn bot."
    elif len(detected_essential) >= 2 or (has_session and has_device):
        grade = "high_quality"
        status_text = "Có đủ token cookie cơ bản. Cần kiểm tra phiên trên trình duyệt; cookie không đảm bảo đăng nhập, chất lượng video hoặc miễn CAPTCHA."
    else:
        grade = "basic"
        status_text = "Cookie cơ bản (thiếu token phiên đăng nhập đầy đủ). Khuyên dùng tài khoản đã đăng nhập."

    return {
        "has_cookie": has_cookie,
        "token_count": len(tokens),
        "preview": mask_cookie_preview(cookie_str),
        "detected_essential": detected_essential,
        "missing_essential": [t for t in essential if t not in keys],
        "grade": grade,
        "status_text": status_text,
    }


def extract_cookies_from_auth_file(auth_path: Path, platform: str | None = None) -> str:
    """Read Playwright auth_state.json and format into standard cookie string.

    Filters cookies by target platform domain to avoid oversized header pollution from third-party sites.
    """
    if not auth_path.exists():
        return ""
    try:
        data = json.loads(auth_path.read_text(encoding="utf-8"))
        cookies = data.get("cookies", [])
        if not cookies or not isinstance(cookies, list):
            return ""

        allowed_domains: tuple[str, ...] | None = None
        if platform == "douyin":
            allowed_domains = ("douyin.com", "iesdouyin.com")
        elif platform == "xiaohongshu":
            allowed_domains = ("xiaohongshu.com", "rednote.com")

        items: list[str] = []
        for c in cookies:
            if not isinstance(c, dict):
                continue
            name = str(c.get("name", "")).strip()
            val = str(c.get("value", ""))
            if not name:
                continue
            if allowed_domains:
                domain = str(c.get("domain", "")).lower().lstrip(".")
                if not any(domain == d or domain.endswith("." + d) for d in allowed_domains):
                    continue
            items.append(f"{name}={val}")

        return "; ".join(items)
    except Exception as err:
        logger.warning("Error reading auth state from %s: %s", auth_path, err)
        return ""


def get_browser_session_cookie(platform: str) -> str:
    """Extract cookies from local browser profile auth state file."""
    if platform == "xiaohongshu":
        auth_file = ROOT / "data/browser/xiaohongshu/auth_state.json"
    elif platform == "douyin":
        auth_file = ROOT / "data/browser/douyin/auth_state.json"
    else:
        return ""
    return extract_cookies_from_auth_file(auth_file, platform=platform)


def clean_and_classify_cookie_string(raw: str) -> dict[str, Any]:
    """Parse raw cookie string, detect target platform, and clean key tokens."""
    tokens = parse_cookie_str(raw)
    if not tokens:
        return {"platform": "unknown", "cookie_str": "", "tokens": {}}

    keys = set(tokens.keys())
    dy_matches = sum(1 for t in DOUYIN_ESSENTIAL_TOKENS if t in keys)
    xhs_matches = sum(1 for t in XHS_ESSENTIAL_TOKENS if t in keys)

    if dy_matches > xhs_matches or "ttwid" in keys or "s_v_web_id" in keys:
        platform = "douyin"
    elif xhs_matches > 0 or "web_session" in keys or "a1" in keys or "webId" in keys:
        platform = "xiaohongshu"
    else:
        platform = "unknown"

    cleaned_str = cookie_dict_to_str(tokens)
    return {
        "platform": platform,
        "cookie_str": cleaned_str,
        "tokens": tokens,
    }
