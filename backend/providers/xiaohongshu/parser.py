"""Only public DOM metadata; never read initial state, cookies or private API responses."""

import math
import re
from urllib.parse import parse_qs, urlsplit

from pydantic import HttpUrl, ValidationError

from backend.schemas.contracts import VideoResult

VALID_HOSTS = {"www.xiaohongshu.com", "xiaohongshu.com", "www.rednote.com", "rednote.com"}


class PageGate(Exception):
    def __init__(self, state: str, message: str):
        super().__init__(message)
        self.state = state


def classify_page(body: str, title: str = "", url: str = "") -> str:
    content = (title + "\n" + body[:6000]).lower()
    split_url = urlsplit(url)
    url_lower = url.lower()
    if (
        any(
            term in content
            for term in ["300012", "ip存在风险", "安全限制", "访问受限", "访问频次异常", "ip at risk"]
        )
        or "error_code=300012" in url_lower
    ):
        return "restricted"
    if any(
        term in content
        for term in ["完成验证", "拖动滑块", "安全验证", "请验证", "验证码验证", "security verification"]
    ):
        return "verification_required"
    if (
        any(
            term in content
            for term in [
                "扫码登录",
                "登录后推荐",
                "登录后查看更多",
                "登录后搜索",
                "手机号登录",
                "scan qr code to log in",
                "scan qr code with rednote to log in",
                "scan qr code",
                "login to explore",
                "log in to explore",
                "log in to view search results",
                "log in with phone number",
                "how to scan with rednote",
                "new users can log in directly",
            ]
        )
        or "/login" in split_url.path
        or "/website-login" in split_url.path
        or "tourist_search" in split_url.query
    ):
        return "login_required"
    return "page_available"


def note_url(raw: str) -> str | None:
    try:
        url = HttpUrl(raw)
    except ValidationError:
        return None
    if (
        url.scheme != "https"
        or url.host not in VALID_HOSTS
        or url.username
        or url.password
        or (url.port is not None and url.port != 443)
    ):
        return None
    match = re.search(
        r"/(?:explore|search_result|discovery/item)/([0-9a-fA-F]{24})(?:/|\?|$)", url.path or ""
    )
    return f"https://www.xiaohongshu.com/explore/{match.group(1).lower()}" if match else None


def is_trusted_xhs_url(raw: str | None) -> bool:
    if not raw:
        return False
    try:
        p = urlsplit(raw)
        if p.scheme != "https":
            return False
        if p.username or p.password:
            return False
        if p.port is not None and p.port != 443:
            return False
        host = (p.hostname or "").lower()
        if not host:
            return False
        trusted = ("xiaohongshu.com", "rednote.com", "xhslink.com")
        return any(host == h or host.endswith("." + h) for h in trusted)
    except Exception:
        return False


def extract_xhs_token(raw_url: str | None) -> str | None:
    """Extracts a non-empty xsec_token from the URL query parameters.

    Ignores URL fragments, unrelated parameters, and empty or whitespace-only tokens.
    If multiple xsec_token parameters exist, picks the last non-empty one.
    """
    if not raw_url:
        return None
    try:
        p = urlsplit(raw_url)
        params = parse_qs(p.query, keep_blank_values=False)
        tokens = params.get("xsec_token", [])
        for val in reversed(tokens):
            cleaned = val.strip()
            if cleaned:
                return cleaned
        return None
    except Exception:
        return None


def has_xhs_token(raw_url: str | None) -> bool:
    """Returns True if the URL contains a valid, non-empty xsec_token query parameter."""
    return extract_xhs_token(raw_url) is not None


def resolve_xhs_urls(
    detail_url: str | None,
    candidate_link: str | None,
) -> tuple[str, str, str]:
    """Resolves (canonical_url, share_url, note_id) for a Xiaohongshu note.

    - canonical_url: Standard explore URL (https://www.xiaohongshu.com/explore/<id>)
    - share_url: Preserves xsec_token from detail_url or candidate_link if trusted,
                 has a valid token query parameter, and matches canonical note ID and path;
                 otherwise falls back to canonical_url.
    - note_id: 24-character hex ID.
    Raises ValueError if neither URL can be resolved to a valid Xiaohongshu note ID.
    """
    detail_canon = note_url(detail_url) if detail_url else None
    cand_canon = note_url(candidate_link) if candidate_link else None

    canonical = detail_canon or cand_canon
    if not canonical:
        raise ValueError("Cannot resolve canonical Xiaohongshu URL from provided links.")

    note_id = canonical.rsplit("/", 1)[1]

    share_url: str
    if (
        detail_url
        and has_xhs_token(detail_url)
        and is_trusted_xhs_url(detail_url)
        and note_url(detail_url) == canonical
    ):
        share_url = detail_url
    elif (
        candidate_link
        and has_xhs_token(candidate_link)
        and is_trusted_xhs_url(candidate_link)
        and note_url(candidate_link) == canonical
    ):
        share_url = candidate_link
    else:
        share_url = canonical

    return canonical, share_url, note_id


def parse_count_text(txt: str | None) -> int | None:
    if not txt:
        return None
    txt = txt.strip()
    if not txt or txt.lower() in {
        "like",
        "reply",
        "collect",
        "chat",
        "share",
        "赞",
        "收藏",
        "评论",
        "分享",
    }:
        return None
    try:
        if txt.endswith("万") or txt.endswith("w") or txt.endswith("W"):
            return int(float(txt[:-1]) * 10000)
        if txt.endswith("k") or txt.endswith("K"):
            return int(float(txt[:-1]) * 1000)
        return int(txt)
    except (ValueError, TypeError):
        return None


def validate_image_url(url_str: str | None) -> str | None:
    if not url_str or not isinstance(url_str, str):
        return None
    url_str = url_str.strip()
    if url_str.startswith("//"):
        url_str = "https:" + url_str
    try:
        image = HttpUrl(url_str)
        host = (image.host or "").lower()
        if image.scheme != "https":
            return None
        valid_domains = (".xhscdn.com", ".rednotecdn.com", ".xiaohongshu.com", ".rednote.com")
        if not any(host.endswith(d) or host == d.lstrip(".") for d in valid_domains):
            return None
        # Exclude generic default placeholder logo
        if "e6214e4fbfae2cf14d634d4296916e8a5eaefdf4" in url_str:
            return None
        return url_str
    except (ValidationError, TypeError):
        return None


def parse_detail(
    raw: dict,
    url: str,
    candidate_cover: str | None = None,
    share_url: str | None = None,
    platform_video_id: str | None = None,
) -> VideoResult | None:
    canonical = note_url(url)
    if not canonical or not raw.get("has_video"):
        return None
    title = raw.get("title")
    caption = raw.get("description")
    if not isinstance(title, str) or not title.strip():
        title = caption[:255] if isinstance(caption, str) else None
    if not title:
        return None
    duration = raw.get("duration")
    if (
        not isinstance(duration, (int, float))
        or isinstance(duration, bool)
        or not math.isfinite(duration)
        or duration <= 0
    ):
        duration = None
    poster = validate_image_url(raw.get("poster"))
    if not poster and candidate_cover:
        poster = validate_image_url(candidate_cover)

    author_name = raw.get("author_name")
    if isinstance(author_name, str):
        author_name = author_name.strip()[:100] or None
    else:
        author_name = None

    author_url = raw.get("author_url")
    author_id = None
    if isinstance(author_url, str):
        m_auth = re.search(r"/user/profile/([0-9a-fA-F]{24})", author_url)
        if m_auth:
            author_id = m_auth.group(1).lower()

    like_count = raw.get("like_count")
    if like_count is None and raw.get("like_text"):
        like_count = parse_count_text(raw.get("like_text"))
    favorite_count = raw.get("favorite_count")
    if favorite_count is None and raw.get("collect_text"):
        favorite_count = parse_count_text(raw.get("collect_text"))
    comment_count = raw.get("comment_count")
    if comment_count is None and raw.get("comment_text"):
        comment_count = parse_count_text(raw.get("comment_text"))

    hashtags = []
    if isinstance(caption, str):
        hashtags = re.findall(r"#([^\s#]+)", caption)

    vid_id = platform_video_id or canonical.rsplit("/", 1)[1]
    final_share_url = share_url if share_url is not None else (url if url != canonical else None)

    return VideoResult(
        platform="xiaohongshu",
        platform_video_id=vid_id,
        url=canonical,
        share_url=final_share_url,
        title=title.strip()[:500],
        caption=caption[:20000] if isinstance(caption, str) else None,
        thumbnail_url=poster,
        author_name=author_name,
        author_id=author_id,
        author_url=author_url,
        hashtags=hashtags,
        duration_seconds=duration,
        like_count=like_count if isinstance(like_count, int) and like_count >= 0 else None,
        favorite_count=favorite_count
        if isinstance(favorite_count, int) and favorite_count >= 0
        else None,
        comment_count=comment_count
        if isinstance(comment_count, int) and comment_count >= 0
        else None,
        is_mock=False,
    )

