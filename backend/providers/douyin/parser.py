import math
import re
from datetime import datetime, timezone
from typing import Any, Literal, cast
from urllib.parse import urlsplit

from pydantic import HttpUrl, ValidationError

from backend.providers.base import apply_search_filters_and_sorting
from backend.schemas.contracts import SearchFilters, VideoResult

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
                "完成验证后继续",
                "请完成下列验证",
                "拖动滑块",
                "按住左边按钮拖动",
                "请完成安全验证",
                "请通过安全验证",
                "please complete the following verification",
                "complete the verification to continue",
                "drag the slider",
                "hold the left button and drag",
            ]
        )
        or re.search(r"/(?:verify|verification|captcha)(?:/|$)", split_url.path.lower())
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
    match = re.search(r"/(?:video|note)/(\d{15,25})(?:/|\?|$)", url.path or "")
    if not match:
        return None
    item_id = match.group(1)
    if "/note/" in (url.path or ""):
        return f"https://www.douyin.com/note/{item_id}"
    return f"https://www.douyin.com/video/{item_id}"


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
        url=cast(HttpUrl, canonical),
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


def extract_douyin_carousel_images(aweme_info: dict) -> list[str]:
    """Extracts carousel image URLs across all known Douyin web schemas."""
    images: list[str] = []

    # Schema 1: image_post_info.images (modern Douyin web schema)
    ipi = aweme_info.get("image_post_info")
    if isinstance(ipi, dict):
        ipi_images = ipi.get("images")
        if isinstance(ipi_images, list):
            for entry in ipi_images:
                if isinstance(entry, dict):
                    disp = entry.get("display_image") or entry.get("origin_image") or entry
                    cand_urls: Any = None
                    if isinstance(disp, dict) and disp.get("url_list"):
                        cand_urls = disp["url_list"]
                    elif isinstance(entry.get("download_addr"), dict) and entry["download_addr"].get("url_list"):
                        cand_urls = entry["download_addr"]["url_list"]
                    elif isinstance(entry.get("url_list"), list):
                        cand_urls = entry["url_list"]

                    if isinstance(cand_urls, list):
                        for u in cand_urls:
                            valid_u = validate_image_url(u)
                            if valid_u:
                                images.append(valid_u)
                                break

    # Schema 2: images list of objects / URLs
    if not images:
        raw_imgs = aweme_info.get("images")
        if isinstance(raw_imgs, list):
            for entry in raw_imgs:
                if isinstance(entry, dict):
                    cand_urls = (
                        entry.get("download_url_list")
                        or (entry.get("download_addr", {}).get("url_list") if isinstance(entry.get("download_addr"), dict) else None)
                        or entry.get("url_list")
                    )
                    if isinstance(cand_urls, list):
                        for u in cand_urls:
                            valid_u = validate_image_url(u)
                            if valid_u:
                                images.append(valid_u)
                                break
                elif isinstance(entry, str):
                    valid_u = validate_image_url(entry)
                    if valid_u:
                        images.append(valid_u)

    # Schema 3: image_list array
    if not images:
        raw_list = aweme_info.get("image_list")
        if isinstance(raw_list, list):
            for entry in raw_list:
                if isinstance(entry, str):
                    valid_u = validate_image_url(entry)
                    if valid_u:
                        images.append(valid_u)
                elif isinstance(entry, dict):
                    cand_urls = entry.get("url_list") or entry.get("download_url_list")
                    if isinstance(cand_urls, list):
                        for u in cand_urls:
                            valid_u = validate_image_url(u)
                            if valid_u:
                                images.append(valid_u)
                                break

    return images


def parse_aweme_item(item: dict) -> VideoResult | None:
    if not isinstance(item, dict):
        return None
    aweme_info = item.get("aweme_info") or item.get("aweme") or item
    if not isinstance(aweme_info, dict):
        return None

    aweme_id = str(aweme_info.get("aweme_id") or "").strip()
    if not aweme_id or not aweme_id.isdigit():
        return None

    desc = str(aweme_info.get("desc") or "").strip()

    create_time = aweme_info.get("create_time")
    published_at: datetime | None = None
    if isinstance(create_time, (int, float)) and create_time > 0:
        try:
            published_at = datetime.fromtimestamp(create_time, tz=timezone.utc)
        except Exception:
            published_at = None

    stats = aweme_info.get("statistics") or {}
    like_count = _parse_metric(stats.get("digg_count"))
    comment_count = _parse_metric(stats.get("comment_count"))
    share_count = _parse_metric(stats.get("share_count"))
    collect_count = _parse_metric(stats.get("collect_count"))
    view_count = _parse_metric(stats.get("play_count"))

    author = aweme_info.get("author") or {}
    author_name = str(author.get("nickname") or author.get("unique_id") or "").strip() or None
    author_id = str(author.get("sec_uid") or author.get("uid") or "").strip() or None
    author_url = f"https://www.douyin.com/user/{author_id}" if author_id else None

    carousel_images = extract_douyin_carousel_images(aweme_info)
    aweme_type = aweme_info.get("aweme_type", 0)
    is_note = bool(carousel_images) or aweme_type in (2, 68, 150)

    images: list[str] = []
    thumbnail_url: str | None = None
    duration_seconds: float | None = None

    if is_note:
        media_type: Literal["video", "image_text"] = "image_text"
        images = carousel_images
        thumbnail_url = images[0] if images else None
        if not thumbnail_url:
            video_obj = aweme_info.get("video") or {}
            cover_obj = video_obj.get("cover") or video_obj.get("origin_cover") or {}
            urls = cover_obj.get("url_list") or []
            for u in urls:
                valid_u = validate_image_url(u)
                if valid_u:
                    thumbnail_url = valid_u
                    break
        canonical_url = f"https://www.douyin.com/note/{aweme_id}"
    else:
        media_type = "video"
        video_obj = aweme_info.get("video") or {}
        duration_ms = video_obj.get("duration")
        if isinstance(duration_ms, (int, float)) and duration_ms > 0:
            duration_seconds = round(float(duration_ms) / 1000.0, 2)
        cover_obj = video_obj.get("cover") or video_obj.get("origin_cover") or {}
        urls = cover_obj.get("url_list") or []
        for u in urls:
            valid_u = validate_image_url(u)
            if valid_u:
                thumbnail_url = valid_u
                break
        canonical_url = f"https://www.douyin.com/video/{aweme_id}"

    hashtags: list[str] = []
    text_extra = aweme_info.get("text_extra") or []
    if isinstance(text_extra, list):
        for te in text_extra:
            if isinstance(te, dict) and te.get("hashtag_name"):
                tag = str(te["hashtag_name"]).strip().lstrip("#")
                if tag and tag not in hashtags:
                    hashtags.append(tag)
    if desc:
        for tag in re.findall(r"#([^\s#]+)", desc):
            clean_tag = tag.strip().lstrip("#")
            if clean_tag and clean_tag not in hashtags:
                hashtags.append(clean_tag)

    title = desc[:500] if desc else f"Douyin {'Note' if is_note else 'Video'} {aweme_id}"

    try:
        return VideoResult(
            platform="douyin",
            platform_video_id=aweme_id,
            url=cast(HttpUrl, canonical_url),
            share_url=canonical_url,
            title=title,
            caption=desc if desc else None,
            author_name=author_name[:100] if author_name else None,
            author_id=author_id,
            author_url=author_url,
            thumbnail_url=thumbnail_url,
            hashtags=hashtags,
            keywords=[],
            duration_seconds=duration_seconds,
            published_at=published_at,
            like_count=like_count,
            comment_count=comment_count,
            share_count=share_count,
            favorite_count=collect_count,
            view_count=view_count,
            media_type=media_type,
            images=images,
            is_mock=False,
        )
    except Exception:
        return None



def parse_search_response_json(
    data: dict,
    limit: int = 50,
    filters: SearchFilters | None = None,
) -> list[VideoResult]:
    if not isinstance(data, dict):
        return []

    raw_list = data.get("data") or data.get("aweme_list") or []
    if not isinstance(raw_list, list):
        return []

    parsed: list[VideoResult] = []
    for item in raw_list:
        vr = parse_aweme_item(item)
        if vr:
            parsed.append(vr)

    return apply_search_filters_and_sorting(parsed, filters=filters, limit=limit)


def _find_items_in_nested_dict(data: Any, max_depth: int = 5) -> list[dict]:
    """Recursively traverses a JSON structure to find aweme or video search items."""
    if max_depth <= 0:
        return []
    if isinstance(data, list):
        found = []
        for it in data:
            if isinstance(it, dict) and (
                "aweme_info" in it or "aweme_id" in it or ("author" in it and "statistics" in it)
            ):
                found.append(it)
        if found:
            return found
        for it in data:
            nested = _find_items_in_nested_dict(it, max_depth - 1)
            if nested:
                return nested
    elif isinstance(data, dict):
        for k in ("data", "aweme_list", "search_data", "cards", "items"):
            if k in data and isinstance(data[k], list):
                res = _find_items_in_nested_dict(data[k], max_depth - 1)
                if res:
                    return res
        for val in data.values():
            if isinstance(val, (dict, list)):
                res = _find_items_in_nested_dict(val, max_depth - 1)
                if res:
                    return res
    return []


def extract_douyin_ssr_json(html_content: str) -> dict[str, Any] | None:
    """
    Extracts server-rendered state JSON from Douyin HTML:
    - <script id="RENDER_DATA"> (URL-encoded JSON)
    - window._ROUTER_DATA = {...}
    - window.__INITIAL_STATE__ = {...}
    Returns a dict with 'data' suitable for parse_search_response_json.
    """
    import json
    from urllib.parse import unquote

    if not html_content:
        return None

    # 1. Check <script id="RENDER_DATA"...>
    m_render = re.search(r'<script\s+id=["\']RENDER_DATA["\'][^>]*>(.*?)</script>', html_content, re.DOTALL)
    if m_render:
        raw_text = m_render.group(1).strip()
        if raw_text:
            try:
                decoded = unquote(raw_text)
                parsed = json.loads(decoded)
                items = _find_items_in_nested_dict(parsed)
                if items:
                    return {"data": items}
            except Exception:
                pass

    # 2. Check window._ROUTER_DATA
    m_router = re.search(r'window\._ROUTER_DATA\s*=\s*(\{.*?\});\s*(?:window|</script>)', html_content, re.DOTALL)
    if m_router:
        try:
            parsed = json.loads(m_router.group(1))
            items = _find_items_in_nested_dict(parsed)
            if items:
                return {"data": items}
        except Exception:
            pass

    # 3. Check window.__INITIAL_STATE__
    m_state = re.search(r'window\.__INITIAL_STATE__\s*=\s*(\{.*?\});\s*(?:window|</script>)', html_content, re.DOTALL)
    if m_state:
        try:
            parsed = json.loads(m_state.group(1))
            items = _find_items_in_nested_dict(parsed)
            if items:
                return {"data": items}
        except Exception:
            pass

    return None
