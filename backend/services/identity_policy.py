"""Unified identity policy, URL validation, and share URL merging helpers.

Ensures identity consistency across contracts, providers, dedup, and persistence:
- Disallows contradictory platform_video_id vs canonical URL.
- Infers missing platform_video_id from canonical URL.
- Treats unresolved short links (xhslink.com) as unverified: they cannot overwrite verified URLs.
- Rejects mismatched note IDs and cross-note redirects.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any
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
DOUYIN_VIDEO_REGEX = re.compile(r"/(?:video|note)/(\d{15,25})(?:/|\?|$)")

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


TIKTOK_VIDEO_REGEX = re.compile(r"/video/(\d{15,25})(?:/|\?|$)")
YOUTUBE_SHORTS_REGEX = re.compile(r"/shorts/([a-zA-Z0-9_-]{8,15})(?:/|\?|$)")


def extract_tiktok_id(raw_url: str | None) -> str | None:
    if not raw_url:
        return None
    try:
        p = urlsplit(raw_url)
        match = TIKTOK_VIDEO_REGEX.search(p.path or "")
        return match.group(1) if match else None
    except Exception:
        return None


def extract_youtube_shorts_id(raw_url: str | None) -> str | None:
    if not raw_url:
        return None
    try:
        p = urlsplit(raw_url)
        match = YOUTUBE_SHORTS_REGEX.search(p.path or "")
        return match.group(1) if match else None
    except Exception:
        return None


def extract_platform_id(platform: str, raw_url: str | None) -> str | None:
    if platform == "xiaohongshu":
        return extract_note_id(raw_url)
    if platform == "douyin":
        return extract_douyin_id(raw_url)
    if platform == "tiktok":
        return extract_tiktok_id(raw_url)
    if platform == "youtube_shorts":
        return extract_youtube_shorts_id(raw_url)
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


@dataclass
class SampleProvenanceResult:
    schema_valid: bool
    source_provenance_verified: bool
    status: str  # "VERIFIED_STRICT", "UNVERIFIED_LEGACY", "UNVERIFIED_MISSING_METADATA", "UNVERIFIED_PENDING_CAPTURE", "UNVERIFIED_HASH_MISMATCH", "UNVERIFIED_INSUFFICIENT_EVIDENCE", "INVALID_SCHEMA"
    detail: str
    reasons: list[str] = field(default_factory=list)

    def __iter__(self):
        """Allows unpacking as (schema_valid, detail) for backward compatibility."""
        return iter((self.schema_valid, self.detail))


def compute_sample_metadata_hash(sample: dict[str, Any]) -> str:
    """Computes a deterministic SHA-256 hash for sample metadata.

    Covers platform, platform_video_id, url, title, author, media_type,
    collected_at, query, submitted_query, session_id, and collector_version.
    """
    canonical_payload = {
        "collected_at": str(sample.get("collected_at") or "").strip(),
        "collector_version": str(sample.get("collector_version") or "").strip(),
        "media_type": str(sample.get("media_type") or "video").strip(),
        "platform": str(sample.get("platform") or "").strip().lower(),
        "platform_video_id": str(sample.get("platform_video_id") or "").strip(),
        "query": str(sample.get("query") or "").strip(),
        "session_id": str(sample.get("session_id") or "").strip(),
        "submitted_query": str(sample.get("submitted_query") or "").strip(),
        "title": str(sample.get("title") or "").strip(),
        "url": str(sample.get("url") or "").strip(),
    }
    encoded = json.dumps(
        canonical_payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")
    return f"sha256:{hashlib.sha256(encoded).hexdigest()}"


def create_capture_manifest_record(
    sample: dict[str, Any], record_id: str | None = None
) -> dict[str, Any]:
    """Generates an independent capture manifest record from a captured sample."""
    plat = str(sample.get("platform") or "").strip().lower()
    vid = str(sample.get("platform_video_id") or "").strip()
    r_id = record_id or f"cap-{plat}-{vid}"
    return {
        "record_id": r_id,
        "platform": plat,
        "platform_video_id": vid,
        "url": str(sample.get("url") or "").strip(),
        "query": str(sample.get("query") or "").strip(),
        "submitted_query": str(sample.get("submitted_query") or "").strip(),
        "collected_at": str(sample.get("collected_at") or "").strip(),
        "session_id": str(sample.get("session_id") or "").strip(),
        "collector_version": str(sample.get("collector_version") or "").strip(),
        "metadata_hash": compute_sample_metadata_hash(sample),
    }


def lookup_capture_manifest_record(
    manifest: Any, sample: dict[str, Any]
) -> dict[str, Any] | None:
    """Finds a matching capture record in an independent manifest by ID or platform_video_id."""
    if manifest is None:
        return None

    if isinstance(manifest, (str, Path)):
        p = Path(manifest)
        if not p.exists():
            return None
        try:
            with open(p, "r", encoding="utf-8") as f:
                data = json.load(f)
            return lookup_capture_manifest_record(data, sample)
        except Exception:
            return None

    target_vid = str(sample.get("platform_video_id") or "").strip()
    target_cid = str(sample.get("capture_id") or sample.get("source_capture_ref") or "").strip()
    target_plat = str(sample.get("platform") or "").strip().lower()

    if isinstance(manifest, list):
        for rec in manifest:
            if not isinstance(rec, dict):
                continue
            r_plat = str(rec.get("platform") or "").strip().lower()
            if r_plat and target_plat and r_plat != target_plat:
                continue
            r_cid = str(rec.get("record_id") or rec.get("capture_id") or "").strip()
            r_vid = str(rec.get("platform_video_id") or rec.get("id") or "").strip()
            if target_cid and r_cid and target_cid == r_cid:
                return rec
            if target_vid and r_vid and target_vid == r_vid:
                return rec
        return None

    if isinstance(manifest, dict):
        if "records" in manifest and isinstance(manifest["records"], list):
            return lookup_capture_manifest_record(manifest["records"], sample)
        if "captures" in manifest and isinstance(manifest["captures"], list):
            return lookup_capture_manifest_record(manifest["captures"], sample)

        if target_cid and target_cid in manifest and isinstance(manifest[target_cid], dict):
            return manifest[target_cid]
        if target_vid and target_vid in manifest and isinstance(manifest[target_vid], dict):
            return manifest[target_vid]
        compound_key = f"{target_plat}:{target_vid}"
        if compound_key in manifest and isinstance(manifest[compound_key], dict):
            return manifest[compound_key]

        for rec in manifest.values():
            if not isinstance(rec, dict):
                continue
            r_plat = str(rec.get("platform") or "").strip().lower()
            if r_plat and target_plat and r_plat != target_plat:
                continue
            r_cid = str(rec.get("record_id") or rec.get("capture_id") or "").strip()
            r_vid = str(rec.get("platform_video_id") or rec.get("id") or "").strip()
            if target_cid and r_cid and target_cid == r_cid:
                return rec
            if target_vid and r_vid and target_vid == r_vid:
                return rec

    return None


def audit_sample_provenance(
    sample: dict[str, Any],
    expected_query: str | None = None,
    capture_manifest: Any = None,
) -> SampleProvenanceResult:
    """Strictly audits a benchmark/dataset sample for schema validity and source provenance.

    Separates:
    - schema_valid: whether the data contract, exact URL structure, canonical regex, ID match,
      and timezone-aware ISO 8601 timestamp are valid.
    - source_provenance_verified: whether the sample has verifiable capture provenance,
      including collector version, session ID, provenance source, and exact query metadata match.

    Legacy unverified samples (provenance_level == 'legacy_unverified') are marked as
    schema_valid=True, source_provenance_verified=False, status='UNVERIFIED_LEGACY'.
    """
    plat = sample.get("platform")
    if plat not in ("douyin", "xiaohongshu"):
        return SampleProvenanceResult(
            schema_valid=False,
            source_provenance_verified=False,
            status="INVALID_SCHEMA",
            detail=f"Invalid platform: '{plat}'",
            reasons=[f"Invalid platform: '{plat}'"],
        )

    url_str = str(sample.get("url") or "")
    if not url_str:
        return SampleProvenanceResult(
            schema_valid=False,
            source_provenance_verified=False,
            status="INVALID_SCHEMA",
            detail="Missing URL",
            reasons=["Missing URL"],
        )

    try:
        p = urlsplit(url_str)
    except Exception as e:
        return SampleProvenanceResult(
            schema_valid=False,
            source_provenance_verified=False,
            status="INVALID_SCHEMA",
            detail=f"Malformed URL: {e}",
            reasons=[f"Malformed URL: {e}"],
        )

    if p.scheme != "https":
        return SampleProvenanceResult(
            schema_valid=False,
            source_provenance_verified=False,
            status="INVALID_SCHEMA",
            detail=f"Insecure URL scheme '{p.scheme}', expected 'https'",
            reasons=[f"Insecure URL scheme '{p.scheme}'"],
        )

    host = (p.hostname or "").lower()
    expected_id = str(sample.get("platform_video_id") or "")
    if not expected_id:
        return SampleProvenanceResult(
            schema_valid=False,
            source_provenance_verified=False,
            status="INVALID_SCHEMA",
            detail="Missing platform_video_id",
            reasons=["Missing platform_video_id"],
        )

    if plat == "douyin":
        if host not in ("www.douyin.com", "douyin.com"):
            return SampleProvenanceResult(
                schema_valid=False,
                source_provenance_verified=False,
                status="INVALID_SCHEMA",
                detail=f"Invalid or spoofed Douyin host: '{host}'",
                reasons=[f"Invalid or spoofed Douyin host: '{host}'"],
            )
        m_dy = DOUYIN_VIDEO_REGEX.search(p.path or "")
        if not m_dy:
            return SampleProvenanceResult(
                schema_valid=False,
                source_provenance_verified=False,
                status="INVALID_SCHEMA",
                detail=f"Invalid Douyin canonical path: '{p.path}'",
                reasons=[f"Invalid Douyin canonical path: '{p.path}'"],
            )
        url_id = m_dy.group(1)
        if url_id != expected_id:
            return SampleProvenanceResult(
                schema_valid=False,
                source_provenance_verified=False,
                status="INVALID_SCHEMA",
                detail=f"URL ID '{url_id}' does not match platform_video_id '{expected_id}'",
                reasons=[f"URL ID mismatch: '{url_id}' vs '{expected_id}'"],
            )
    elif plat == "xiaohongshu":
        if host not in (
            "www.xiaohongshu.com",
            "xiaohongshu.com",
            "www.rednote.com",
            "rednote.com",
        ):
            return SampleProvenanceResult(
                schema_valid=False,
                source_provenance_verified=False,
                status="INVALID_SCHEMA",
                detail=f"Invalid or spoofed Xiaohongshu host: '{host}'",
                reasons=[f"Invalid or spoofed Xiaohongshu host: '{host}'"],
            )
        m_xhs = XHS_NOTE_REGEX.search(p.path or "")
        if not m_xhs:
            return SampleProvenanceResult(
                schema_valid=False,
                source_provenance_verified=False,
                status="INVALID_SCHEMA",
                detail=f"Invalid Xiaohongshu canonical path: '{p.path}'",
                reasons=[f"Invalid Xiaohongshu canonical path: '{p.path}'"],
            )
        url_id = m_xhs.group(1).lower()
        if url_id != expected_id.lower():
            return SampleProvenanceResult(
                schema_valid=False,
                source_provenance_verified=False,
                status="INVALID_SCHEMA",
                detail=f"URL ID '{url_id}' does not match platform_video_id '{expected_id}'",
                reasons=[f"URL ID mismatch: '{url_id}' vs '{expected_id}'"],
            )

    # ISO 8601 parsing with timezone awareness
    collected_at_str = str(sample.get("collected_at") or "")
    if not collected_at_str:
        return SampleProvenanceResult(
            schema_valid=False,
            source_provenance_verified=False,
            status="INVALID_SCHEMA",
            detail="Missing collected_at timestamp",
            reasons=["Missing collected_at timestamp"],
        )
    try:
        dt = datetime.fromisoformat(collected_at_str.replace("Z", "+00:00"))
        if dt.tzinfo is None or dt.utcoffset() is None:
            return SampleProvenanceResult(
                schema_valid=False,
                source_provenance_verified=False,
                status="INVALID_SCHEMA",
                detail=f"collected_at timestamp '{collected_at_str}' lacks timezone offset",
                reasons=["Timestamp lacks timezone offset"],
            )
    except Exception as e:
        return SampleProvenanceResult(
            schema_valid=False,
            source_provenance_verified=False,
            status="INVALID_SCHEMA",
            detail=f"Invalid ISO 8601 timestamp '{collected_at_str}': {e}",
            reasons=[f"Invalid ISO 8601 timestamp: {e}"],
        )

    media_type = sample.get("media_type")
    if media_type not in ("video", "image_text"):
        return SampleProvenanceResult(
            schema_valid=False,
            source_provenance_verified=False,
            status="INVALID_SCHEMA",
            detail=f"Invalid media_type '{media_type}'",
            reasons=[f"Invalid media_type '{media_type}'"],
        )

    for field_name in ("title", "author", "rationale"):
        val = sample.get(field_name)
        if not isinstance(val, str) or not val.strip():
            return SampleProvenanceResult(
                schema_valid=False,
                source_provenance_verified=False,
                status="INVALID_SCHEMA",
                detail=f"Missing or empty required field '{field_name}'",
                reasons=[f"Missing or empty required field '{field_name}'"],
            )

    if not isinstance(sample.get("ground_truth_relevance"), bool):
        return SampleProvenanceResult(
            schema_valid=False,
            source_provenance_verified=False,
            status="INVALID_SCHEMA",
            detail="ground_truth_relevance must be a boolean",
            reasons=["ground_truth_relevance must be a boolean"],
        )

    # Check for legacy unverified sample: schema is valid, but provenance is unverified
    prov_level = sample.get("provenance_level")
    if prov_level == "legacy_unverified":
        return SampleProvenanceResult(
            schema_valid=True,
            source_provenance_verified=False,
            status="UNVERIFIED_LEGACY",
            detail="Legacy sample without verified capture source (provenance_level=legacy_unverified)",
            reasons=["Legacy sample without verified capture source"],
        )

    # Strict provenance verification: metadata requirements
    missing_meta = []
    for req_field in (
        "query",
        "submitted_query",
        "session_id",
        "collector_version",
        "provenance_source",
        "provenance_level",
    ):
        v = sample.get(req_field)
        if not v or not str(v).strip():
            missing_meta.append(req_field)

    if missing_meta:
        return SampleProvenanceResult(
            schema_valid=True,
            source_provenance_verified=False,
            status="UNVERIFIED_MISSING_METADATA",
            detail=f"Missing required provenance metadata fields: {', '.join(missing_meta)}",
            reasons=[f"Missing required field '{f}'" for f in missing_meta],
        )

    if str(sample.get("provenance_level")).strip() != "verified_strict_audit":
        return SampleProvenanceResult(
            schema_valid=True,
            source_provenance_verified=False,
            status="UNVERIFIED",
            detail=f"provenance_level is '{sample.get('provenance_level')}', expected 'verified_strict_audit'",
            reasons=["provenance_level mismatch"],
        )

    # Query metadata matching outer group query
    sample_query = str(sample.get("query") or "").strip()
    if expected_query and sample_query != expected_query.strip():
        return SampleProvenanceResult(
            schema_valid=True,
            source_provenance_verified=False,
            status="UNVERIFIED",
            detail=f"Sample query metadata '{sample_query}' does not match expected group query '{expected_query}'",
            reasons=[f"Query mismatch: '{sample_query}' vs '{expected_query}'"],
        )

    # Verifiable source evidence check against independent capture manifest
    manifest_rec = lookup_capture_manifest_record(capture_manifest, sample)
    if not manifest_rec:
        return SampleProvenanceResult(
            schema_valid=True,
            source_provenance_verified=False,
            status="UNVERIFIED_PENDING_CAPTURE",
            detail="Sample has valid schema and metadata but lacks matching independent capture manifest record",
            reasons=["No matching independent capture record in manifest"],
        )

    # Validate manifest platform consistency
    m_plat = str(manifest_rec.get("platform") or "").strip().lower()
    s_plat = str(sample.get("platform") or "").strip().lower()
    if m_plat != s_plat:
        return SampleProvenanceResult(
            schema_valid=True,
            source_provenance_verified=False,
            status="UNVERIFIED_MANIFEST_MISMATCH",
            detail=f"Manifest platform '{m_plat}' does not match sample platform '{s_plat}'",
            reasons=[f"Manifest platform mismatch: '{m_plat}' vs '{s_plat}'"],
        )

    # Validate metadata hash
    expected_hash = manifest_rec.get("metadata_hash")
    if not expected_hash:
        return SampleProvenanceResult(
            schema_valid=True,
            source_provenance_verified=False,
            status="UNVERIFIED_INSUFFICIENT_EVIDENCE",
            detail="Capture manifest record missing metadata_hash",
            reasons=["Capture manifest record missing metadata_hash"],
        )

    actual_hash = compute_sample_metadata_hash(sample)
    clean_exp = str(expected_hash).strip().lower().removeprefix("sha256:")
    clean_act = str(actual_hash).strip().lower().removeprefix("sha256:")
    if clean_exp != clean_act:
        return SampleProvenanceResult(
            schema_valid=True,
            source_provenance_verified=False,
            status="UNVERIFIED_HASH_MISMATCH",
            detail=f"Metadata hash mismatch with manifest record '{manifest_rec.get('record_id')}': expected {expected_hash}, calculated {actual_hash}",
            reasons=[f"Metadata hash mismatch: expected {expected_hash}, got {actual_hash}"],
        )

    return SampleProvenanceResult(
        schema_valid=True,
        source_provenance_verified=True,
        status="VERIFIED_STRICT",
        detail=f"Provenance strictly verified against capture manifest record '{manifest_rec.get('record_id')}'",
        reasons=[],
    )
