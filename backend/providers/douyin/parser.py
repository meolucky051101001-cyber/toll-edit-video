"""Only public DOM metadata for Douyin; never read initial state, cookies or private API responses."""

import math
import re
from urllib.parse import urlsplit

from pydantic import HttpUrl, ValidationError

from backend.schemas.contracts import VideoResult

VALID_HOSTS = {"www.douyin.com", "douyin.com"}


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
            for term in [
                "访问过于频繁",
                "系统繁忙",
                "ip存在风险",
                "安全限制",
                "访问受限",
                "访问频次异常",
                "risk",
            ]
        )
        or "error_code=" in url_lower
    ):
        return "restricted"

    if (
        any(
            term in content
            for term in [
                "验证码中间页",
                "完成验证",
                "安全验证",
                "请完成下列验证",
                "拖动滑块",
                "按住左边按钮拖动",
                "拼图",
                "verify",
                "security verification",
            ]
        )
        or "verify" in split_url.path.lower()
    ):
        return "verification_required"

    if (
        any(
            term in content
            for term in [
                "扫码登录",
                "登录后查看",
                "登录后查看更多",
                "登录后搜索",
                "手机号登录",
                "scan qr code to log in",
                "login",
                "立即登录",
            ]
        )
        or "/login" in split_url.path
        or "passport.douyin.com" in split_url.netloc
    ):
        return "login_required"

    return "page_available"


def video_url(raw: str) -> str | None:
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
    match = re.search(r"/video/(\d{15,25})(?:/|\?|$)", url.path or "")
    return f"https://www.douyin.com/video/{match.group(1)}" if match else None


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
        clean = txt.replace(",", "")
        if clean.endswith("万") or clean.endswith("w") or clean.endswith("W"):
            return int(float(clean[:-1]) * 10000)
        if clean.endswith("k") or clean.endswith("K"):
            return int(float(clean[:-1]) * 1000)
        return int(clean)
    except (ValueError, TypeError):
        return None


def _parse_metric(val: object) -> int | None:
    if val is None:
        return None
    if isinstance(val, bool):
        return None
    if isinstance(val, int):
        return val if val >= 0 else None
    if isinstance(val, float):
        return int(val) if math.isfinite(val) and val >= 0 else None
    if isinstance(val, str):
        return parse_count_text(val)
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
        valid_domains = (
            ".byteimg.com",
            ".douyinpic.com",
            ".pstatp.com",
            ".douyincdn.com",
            ".volces.com",
            ".douyin.com",
        )
        if not any(host.endswith(d) or host == d.lstrip(".") for d in valid_domains):
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
    canonical = video_url(url)
    if not canonical or not raw.get("has_video"):
        return None
    title = raw.get("title")
    caption = raw.get("caption") or raw.get("description")
    if not isinstance(title, str) or not title.strip():
        title = caption[:255] if isinstance(caption, str) else None
    if not title:
        return None

    duration_val = (
        raw.get("duration_seconds")
        if raw.get("duration_seconds") is not None
        else raw.get("duration")
    )
    duration: float | None = None
    if (
        isinstance(duration_val, (int, float))
        and not isinstance(duration_val, bool)
        and math.isfinite(duration_val)
        and duration_val > 0
    ):
        duration = float(duration_val)
    elif isinstance(duration_val, str):
        try:
            parsed_d = float(duration_val.strip())
            if math.isfinite(parsed_d) and parsed_d > 0:
                duration = parsed_d
        except (ValueError, TypeError):
            pass

    poster = validate_image_url(
        raw.get("thumbnail_url") or raw.get("poster") or candidate_cover
    )

    author_name = raw.get("author_name")
    if isinstance(author_name, str):
        author_name = author_name.strip()[:100] or None
    else:
        author_name = None

    author_url = raw.get("author_url")
    author_id = None
    if isinstance(author_url, str):
        m_auth = re.search(r"/user/([a-zA-Z0-9_\-]+)", author_url)
        if m_auth:
            author_id = m_auth.group(1)

    like_count = _parse_metric(raw.get("like_count"))
    if like_count is None and raw.get("like_text"):
        like_count = _parse_metric(raw.get("like_text"))

    favorite_count = _parse_metric(raw.get("favorite_count"))
    if favorite_count is None and raw.get("collect_text"):
        favorite_count = _parse_metric(raw.get("collect_text"))

    comment_count = _parse_metric(raw.get("comment_count"))
    if comment_count is None and raw.get("comment_text"):
        comment_count = _parse_metric(raw.get("comment_text"))

    share_count = _parse_metric(raw.get("share_count"))
    if share_count is None and raw.get("share_text"):
        share_count = _parse_metric(raw.get("share_text"))

    raw_hashtags = raw.get("hashtags")
    hashtags: list[str] = []
    if isinstance(raw_hashtags, list):
        for tag in raw_hashtags:
            if isinstance(tag, str) and tag.strip():
                clean_tag = tag.strip().lstrip("#")
                if clean_tag and clean_tag not in hashtags:
                    hashtags.append(clean_tag)
    if isinstance(caption, str):
        for tag in re.findall(r"#([^\s#]+)", caption):
            if tag and tag not in hashtags:
                hashtags.append(tag)

    vid_id = platform_video_id or canonical.rsplit("/", 1)[1]
    final_share_url = share_url or canonical

    return VideoResult(
        platform="douyin",
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
        like_count=like_count,
        favorite_count=favorite_count,
        comment_count=comment_count,
        share_count=share_count,
    )

