"""Unified identity policy, URL validation, and share URL merging helpers.

Ensures identity consistency across contracts, providers, dedup, and persistence:
- Disallows contradictory platform_video_id vs canonical URL.
- Infers missing platform_video_id from canonical URL.
- Treats unresolved short links (xhslink.com) as unverified: they cannot overwrite verified URLs.
- Rejects mismatched note IDs and cross-note redirects.
"""

from __future__ import annotations

import logging
import re
from urllib.parse import parse_qs, urljoin, urlsplit

from backend.services.download.network import (
    create_safe_async_client,
    create_safe_sync_client,
    resolve_and_validate_host_sync,
)

logger = logging.getLogger(__name__)

XHS_NOTE_REGEX = re.compile(
    r"/(?:explore|search_result|discovery/item)/([0-9a-fA-F]{24})(?:/|\?|$)"
)
DOUYIN_VIDEO_REGEX = re.compile(r"/video/(\d{15,25})(?:/|\?|$)")

XHS_NOTE_HOSTS = ("xiaohongshu.com", "rednote.com")
XHS_TRUSTED_HOSTS = ("xiaohongshu.com", "rednote.com", "xhslink.com")
DOUYIN_TRUSTED_HOSTS = ("douyin.com", "iesdouyin.com")


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
        return any(host == h or host.endswith("." + h) for h in XHS_TRUSTED_HOSTS)
    except Exception:
        return False


def is_xhs_short_link(raw: str | None) -> bool:
    if not raw:
        return False
    try:
        p = urlsplit(raw)
        if p.scheme != "https":
            return False
        host = (p.hostname or "").lower()
        return host == "xhslink.com" or host.endswith(".xhslink.com")
    except Exception:
        return False


def extract_note_id(raw_url: str | None) -> str | None:
    if not raw_url:
        return None
    try:
        p = urlsplit(raw_url)
        if p.scheme != "https":
            return None
        host = (p.hostname or "").lower()
        if not any(host == h or host.endswith("." + h) for h in XHS_NOTE_HOSTS):
            return None
        match = XHS_NOTE_REGEX.search(p.path or "")
        return match.group(1).lower() if match else None
    except Exception:
        return None


def extract_douyin_id(raw_url: str | None) -> str | None:
    if not raw_url:
        return None
    try:
        p = urlsplit(raw_url)
        if p.scheme != "https":
            return None
        host = (p.hostname or "").lower()
        if not any(host == h or host.endswith("." + h) for h in DOUYIN_TRUSTED_HOSTS):
            return None
        match = DOUYIN_VIDEO_REGEX.search(p.path or "")
        return match.group(1) if match else None
    except Exception:
        return None


def extract_platform_id(platform: str, raw_url: str | None) -> str | None:
    if platform == "xiaohongshu":
        return extract_note_id(raw_url)
    if platform == "douyin":
        return extract_douyin_id(raw_url)
    return None


def extract_xhs_token(raw_url: str | None) -> str | None:
    """Extracts a non-empty xsec_token from the URL query parameters.

    Ignores URL fragments, unrelated parameters, and empty or whitespace-only tokens.
    If multiple xsec_token parameters exist, picks the last non-empty one.
    """
    if not raw_url:
        return None
    try:
        p = urlsplit(raw_url)
        if not p.query:
            return None
        params = parse_qs(p.query, keep_blank_values=False)
        tokens = params.get("xsec_token")
        if not tokens:
            return None
        for tok in reversed(tokens):
            clean = tok.strip()
            if clean:
                return clean
        return None
    except Exception:
        return None


def has_xhs_token(raw_url: str | None) -> bool:
    return extract_xhs_token(raw_url) is not None


def validate_result_identity(
    platform: str,
    platform_video_id: str | None,
    url: str,
    share_url: str | None = None,
    is_mock: bool = False,
) -> tuple[bool, str | None, str | None]:
    """Validates mutual identity consistency among platform_video_id, url, and share_url.

    Returns:
        (is_valid, resolved_id, error_message)
    """
    if is_mock:
        return True, platform_video_id, None

    url_id = extract_platform_id(platform, url)
    resolved_id = platform_video_id

    if url_id:
        if platform_video_id:
            norm_pvid = platform_video_id.strip()
            if platform == "xiaohongshu":
                norm_pvid = norm_pvid.lower()
                norm_url_id = url_id.lower()
                if norm_pvid != norm_url_id:
                    return (
                        False,
                        None,
                        f"Mâu thuẫn danh tính: platform_video_id '{platform_video_id}' khác ID trong url '{url_id}'.",
                    )
            elif platform == "douyin":
                if norm_pvid.isdigit() and norm_pvid != url_id:
                    return (
                        False,
                        None,
                        f"Mâu thuẫn danh tính: platform_video_id '{platform_video_id}' khác ID trong url '{url_id}'.",
                    )
            else:
                if norm_pvid != url_id:
                    return (
                        False,
                        None,
                        f"Mâu thuẫn danh tính: platform_video_id '{platform_video_id}' khác ID trong url '{url_id}'.",
                    )
        else:
            resolved_id = url_id
    elif not platform_video_id:
        resolved_id = None

    return True, resolved_id, None


def is_safe_xhs_redirect_url(raw: str | None) -> bool:
    """Verifies that the URL is a trusted XHS domain, uses HTTPS, and does not resolve to a private/internal IP."""
    if not raw or not is_trusted_xhs_url(raw):
        return False
    try:
        p = urlsplit(raw)
        hostname = p.hostname
        if not hostname:
            return False
        resolve_and_validate_host_sync(hostname, p.port or 443)
        return True
    except Exception:
        return False


def resolve_xhs_short_link_sync(
    url: str, expected_id: str | None = None, timeout: float = 3.0
) -> str | None:
    if not is_xhs_short_link(url):
        return None
    if not is_safe_xhs_redirect_url(url):
        return None

    current_url = url
    try:
        with create_safe_sync_client(
            timeout=timeout,
            headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"},
        ) as client:
            for _ in range(3):
                resp = client.head(current_url)
                status_code = getattr(resp, "status_code", 200)
                headers = getattr(resp, "headers", {}) or {}
                if status_code in (301, 302, 303, 307, 308) and "location" in headers:
                    loc = headers.get("location")
                    if not loc:
                        return None
                    next_url = urljoin(current_url, loc)
                    if not is_safe_xhs_redirect_url(next_url):
                        return None
                    current_url = next_url
                elif getattr(resp, "url", None) and str(resp.url) != current_url:
                    candidate = str(resp.url)
                    if not is_safe_xhs_redirect_url(candidate):
                        return None
                    current_url = candidate
                    break
                else:
                    break

            target_id = extract_note_id(current_url)
            if not target_id:
                return None
            if expected_id and target_id.lower() != expected_id.lower():
                return None
            return current_url
    except Exception:
        return None


async def resolve_xhs_short_link_async(
    url: str, expected_id: str | None = None, timeout: float = 3.0
) -> str | None:
    if not is_xhs_short_link(url):
        return None
    if not is_safe_xhs_redirect_url(url):
        return None

    current_url = url
    try:
        async with create_safe_async_client(
            timeout=timeout,
            headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"},
        ) as client:
            for _ in range(3):
                resp = await client.head(current_url)
                status_code = getattr(resp, "status_code", 200)
                headers = getattr(resp, "headers", {}) or {}
                if status_code in (301, 302, 303, 307, 308) and "location" in headers:
                    loc = headers.get("location")
                    if not loc:
                        return None
                    next_url = urljoin(current_url, loc)
                    if not is_safe_xhs_redirect_url(next_url):
                        return None
                    current_url = next_url
                elif getattr(resp, "url", None) and str(resp.url) != current_url:
                    candidate = str(resp.url)
                    if not is_safe_xhs_redirect_url(candidate):
                        return None
                    current_url = candidate
                    break
                else:
                    break

            target_id = extract_note_id(current_url)
            if not target_id:
                return None
            if expected_id and target_id.lower() != expected_id.lower():
                return None
            return current_url
    except Exception:
        return None


def merge_share_url(
    platform: str,
    platform_video_id: str | None,
    record_canonical_url: str | None,
    existing_share_url: str | None,
    incoming_share_url: str | None,
) -> str | None:
    """Safely merges incoming share_url into an existing video record.

    Enforces strict identity rules:
    - Rejects contradictory platform_video_id vs canonical URL (returns None, never fabricates URL).
    - Differentiates verified note URLs from unverified short URLs (xhslink.com).
    - Unresolved short URLs cannot overwrite verified note/canonical URLs.
    - Token on a short link does NOT count as a verified token for this note.
    - Preserves existing verified token URL when incoming lacks verified token for this note.
    - Replaces with incoming token URL only if verified for this note ID.
    - Falls back to validated canonical URL if available.
    """
    if platform != "xiaohongshu":
        return incoming_share_url or existing_share_url

    expected_id: str | None = None
    if platform_video_id and re.fullmatch(r"[0-9a-fA-F]{24}", platform_video_id.strip()):
        expected_id = platform_video_id.strip().lower()

    if record_canonical_url:
        canon_id = extract_note_id(record_canonical_url)
        if canon_id:
            if expected_id is not None and canon_id != expected_id:
                # Contradiction: platform_video_id B vs canonical A!
                return None
            if expected_id is None:
                expected_id = canon_id

    if not expected_id and existing_share_url:
        ext_id = extract_note_id(existing_share_url)
        if ext_id:
            expected_id = ext_id

    # If neither expected_id nor record_canonical_url can be verified, no safe merge
    if not expected_id:
        return None

    validated_record_canonical: str | None = None
    if record_canonical_url:
        c_id = extract_note_id(record_canonical_url)
        if c_id and c_id == expected_id:
            validated_record_canonical = f"https://www.xiaohongshu.com/explore/{expected_id}"
    elif expected_id:
        validated_record_canonical = f"https://www.xiaohongshu.com/explore/{expected_id}"

    # Evaluate candidates:
    # Returns (is_valid, has_verified_token, is_verified_note, is_short)
    def _evaluate(candidate: str | None) -> tuple[bool, bool, bool, bool]:
        if not candidate:
            return False, False, False, False
        if not is_trusted_xhs_url(candidate):
            return False, False, False, False

        if is_xhs_short_link(candidate):
            # Short link is unverified without resolution.
            # Token parameter on a short link does not prove identity for expected_id.
            return True, False, False, True

        note_id = extract_note_id(candidate)
        if not note_id or note_id != expected_id:
            # Belongs to a different note ID or malformed -> invalid
            return False, False, False, False

        has_token = has_xhs_token(candidate)
        return True, has_token, True, False

    inc_valid, inc_token, inc_note, inc_short = _evaluate(incoming_share_url)
    ext_valid, ext_token, ext_note, ext_short = _evaluate(existing_share_url)

    # Priority 1: Verified note URL with valid token for expected_id
    if inc_valid and inc_note and inc_token:
        return incoming_share_url
    if ext_valid and ext_note and ext_token:
        return existing_share_url

    # Priority 2: Verified canonical note URL for expected_id
    if inc_valid and inc_note:
        return incoming_share_url
    if ext_valid and ext_note:
        return existing_share_url

    # Priority 3: Unresolved short link
    # (allowed on initial creation if no verified existing URL, but NEVER overwrites verified note/token URL)
    if inc_valid and inc_short and not (ext_valid and ext_note):
        return incoming_share_url
    if ext_valid and ext_short:
        return existing_share_url

    if not incoming_share_url and not existing_share_url:
        return None

    return validated_record_canonical
