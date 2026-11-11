"""Allowlist of media CDN hosts that may receive platform session cookies.

Media URLs come from upstream API responses and can be attacker-influenced;
cookies must never be sent to a host outside the platform's own CDN family.
"""

from urllib.parse import urlsplit

PLATFORM_MEDIA_DOMAINS: dict[str, tuple[str, ...]] = {
    "douyin": (
        "douyin.com",
        "douyinvod.com",
        "snssdk.com",
        "bytecdn.cn",
        "amemv.com",
        "zjcdn.com",
    ),
    "xiaohongshu": (
        "xhscdn.com",
        "xiaohongshu.com",
        "rednote.com",
    ),
}


def cookie_allowed(platform: str, url: str) -> bool:
    domains = PLATFORM_MEDIA_DOMAINS.get(platform)
    if not domains:
        return False
    try:
        parts = urlsplit(url)
    except ValueError:
        return False
    if parts.scheme not in ("http", "https"):
        return False
    host = (parts.hostname or "").lower().rstrip(".")
    if not host:
        return False
    return any(host == domain or host.endswith("." + domain) for domain in domains)