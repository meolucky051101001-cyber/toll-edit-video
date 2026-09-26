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
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urljoin, urlsplit
from uuid import uuid4

from backend.services.download.network import (
    create_safe_async_client,
    create_safe_sync_client,
    resolve_and_validate_host_sync,
)

logger = logging.getLogger(__name__)

ROOT_DIR = Path(__file__).resolve().parent.parent.parent
DEFAULT_LIVE_MANIFEST_PATH = ROOT_DIR / "data" / "manifests" / "live_capture_manifest.jsonl"
DEFAULT_METADATA_HASH_VERSION = "v2"
DEFAULT_ANNOTATION_HASH_VERSION = "v1"

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
    annotation_verified: bool = False
    manifest_record_id: str | None = None

    def __iter__(self):
        """Allows unpacking as (schema_valid, detail) for backward compatibility."""
        return iter((self.schema_valid, self.detail))


def compute_sample_metadata_hash(
    sample: dict[str, Any], version: str = DEFAULT_METADATA_HASH_VERSION
) -> str:
    """Computes a deterministic SHA-256 hash for raw crawl capture metadata.

    Version v2 (default): Covers author, caption, collected_at, collector_version,
    hash_version, hashtags, like_count, media_type, platform, platform_video_id,
    query, session_id, submitted_query, title, and url.
    Strictly excludes benchmark annotation labels (ground_truth_relevance, rationale),
    which are hashed independently to guarantee separation of crawl and labeling data.

    Version v1 (legacy compatibility): Covers collected_at, collector_version, media_type,
    platform, platform_video_id, query, session_id, submitted_query, title, url.
    """
    canonical_payload: dict[str, Any]
    if version == "v1":
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
    else:
        raw_hashtags = sample.get("hashtags") or []
        if isinstance(raw_hashtags, list):
            clean_hashtags = sorted([str(h).strip() for h in raw_hashtags if str(h).strip()])
        else:
            clean_hashtags = []

        like_val = sample.get("like_count")
        if like_val is not None:
            try:
                like_count = int(like_val)
            except (ValueError, TypeError):
                like_count = None
        else:
            like_count = None

        canonical_payload = {
            "author": str(sample.get("author") or sample.get("author_name") or "").strip(),
            "caption": str(sample.get("caption") or "").strip(),
            "collected_at": str(sample.get("collected_at") or "").strip(),
            "collector_version": str(sample.get("collector_version") or "").strip(),
            "hash_version": "v2",
            "hashtags": clean_hashtags,
            "like_count": like_count,
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


def compute_sample_annotation_hash(
    annotation: dict[str, Any], version: str = DEFAULT_ANNOTATION_HASH_VERSION
) -> str:
    """Computes a deterministic SHA-256 hash for benchmark annotation labels.

    Decoupled from raw crawl capture metadata: covers ground_truth_relevance,
    rationale, reviewer, reviewed_at timestamp, and target_video_id.
    """
    canonical_payload = {
        "annotation_version": version,
        "ground_truth_relevance": bool(annotation.get("ground_truth_relevance")),
        "platform": str(annotation.get("platform") or "").strip().lower(),
        "platform_video_id": str(
            annotation.get("platform_video_id") or annotation.get("id") or ""
        ).strip(),
        "rationale": str(annotation.get("rationale") or "").strip(),
        "reviewed_at": str(
            annotation.get("reviewed_at")
            or annotation.get("annotated_at")
            or annotation.get("collected_at")
            or ""
        ).strip(),
        "reviewer": str(
            annotation.get("reviewer")
            or annotation.get("annotator")
            or "curator"
        ).strip(),
    }
    encoded = json.dumps(
        canonical_payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")
    return f"sha256:{hashlib.sha256(encoded).hexdigest()}"


def create_annotation_record(
    sample: dict[str, Any],
    reviewer: str = "benchmark_reviewer",
    reviewed_at: str | None = None,
    version: str = DEFAULT_ANNOTATION_HASH_VERSION,
) -> dict[str, Any]:
    """Generates an independent benchmark annotation record with verifiable annotation hash."""
    plat = str(sample.get("platform") or "").strip().lower()
    vid = str(sample.get("platform_video_id") or "").strip()
    r_at = reviewed_at or str(sample.get("collected_at") or "").strip()
    rec = {
        "record_id": f"ann-{plat}-{vid}",
        "platform": plat,
        "platform_video_id": vid,
        "ground_truth_relevance": bool(sample.get("ground_truth_relevance")),
        "rationale": str(sample.get("rationale") or "").strip(),
        "reviewer": reviewer,
        "reviewed_at": r_at,
        "annotation_version": version,
    }
    rec["annotation_hash"] = compute_sample_annotation_hash(rec, version=version)
    return rec


def audit_sample_annotation(
    sample: dict[str, Any],
    annotation_record: dict[str, Any] | None = None,
) -> tuple[bool, str]:
    """Audits annotation integrity between a sample and its independent annotation record.

    Detects if ground_truth_relevance or rationale was tampered with or modified.
    """
    if annotation_record is not None:
        rec_gt = bool(annotation_record.get("ground_truth_relevance"))
        sample_gt = bool(sample.get("ground_truth_relevance"))
        if rec_gt != sample_gt:
            return False, f"Annotation relevance mismatch: sample has {sample_gt} but record has {rec_gt}"
        rec_rat = str(annotation_record.get("rationale") or "").strip()
        sample_rat = str(sample.get("rationale") or "").strip()
        if rec_rat != sample_rat:
            return False, f"Annotation rationale mismatch: sample '{sample_rat}' vs record '{rec_rat}'"
        expected_hash = annotation_record.get("annotation_hash")
        computed_hash = compute_sample_annotation_hash(annotation_record)
        if expected_hash and expected_hash != computed_hash:
            return False, f"Annotation record hash mismatch: expected {expected_hash}, calculated {computed_hash}"
        return True, f"Annotation verified against record '{annotation_record.get('record_id')}'"

    # If sample itself has annotation_hash
    sample_hash = sample.get("annotation_hash")
    if sample_hash:
        computed = compute_sample_annotation_hash(sample)
        if sample_hash != computed:
            return False, f"Sample annotation hash mismatch: expected {sample_hash}, calculated {computed}"
        return True, "Sample annotation hash verified"

    return False, "Missing annotation record or annotation_hash"


def append_live_capture_manifest_record(
    record: dict[str, Any],
    manifest_path: Path | str | None = None,
) -> Path:
    """Appends a verifiable capture record to an independent append-only JSONL manifest file."""
    p = Path(manifest_path) if manifest_path else DEFAULT_LIVE_MANIFEST_PATH
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")
    return p


def record_live_crawl_capture(
    item: Any,
    query: str,
    submitted_query: str,
    session_id: str | None = None,
    collector_version: str = "v1.2.0-live",
    manifest_path: Path | str | None = None,
) -> dict[str, Any]:
    """Records a live crawl event into an independent append-only capture manifest.

    NOTE: Must ONLY be called when ingesting real provider/crawl events, NEVER for
    retroactively fabricating provenance for old unverified fixtures.
    """
    if isinstance(item, dict):
        platform = str(item.get("platform") or "").strip().lower()
        vid = str(item.get("platform_video_id") or "").strip()
        url = str(item.get("url") or "").strip()
        title = str(item.get("title") or "").strip()
        author = str(item.get("author") or item.get("author_name") or "").strip()
        caption = str(item.get("caption") or "").strip()
        hashtags = list(item.get("hashtags") or [])
        like_count = item.get("like_count")
        media_type = str(item.get("media_type") or "video").strip()
    else:
        platform = str(getattr(item, "platform", "") or "").strip().lower()
        vid = str(getattr(item, "platform_video_id", "") or "").strip()
        url = str(getattr(item, "url", "") or "").strip()
        title = str(getattr(item, "title", "") or "").strip()
        author = str(getattr(item, "author_name", "") or getattr(item, "author", "") or "").strip()
        caption = str(getattr(item, "caption", "") or "").strip()
        hashtags = list(getattr(item, "hashtags", []) or [])
        like_count = getattr(item, "like_count", None)
        media_type = str(getattr(item, "media_type", "video") or "video").strip()

    collected_at = datetime.now(timezone.utc).isoformat()
    sess_id = session_id or f"live-crawl-{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')}"
    rec_id = f"cap-{platform}-{vid}-{uuid4().hex[:8]}"

    sample_repr = {
        "author": author,
        "caption": caption,
        "collected_at": collected_at,
        "collector_version": collector_version,
        "hashtags": hashtags,
        "like_count": like_count,
        "media_type": media_type,
        "platform": platform,
        "platform_video_id": vid,
        "query": query,
        "session_id": sess_id,
        "submitted_query": submitted_query,
        "title": title,
        "url": url,
    }
    meta_hash = compute_sample_metadata_hash(sample_repr, version="v2")

    record = {
        "record_id": rec_id,
        "platform": platform,
        "platform_video_id": vid,
        "url": url,
        "query": query,
        "submitted_query": submitted_query,
        "collected_at": collected_at,
        "session_id": sess_id,
        "collector_version": collector_version,
        "author": author,
        "title": title,
        "caption": caption,
        "hashtags": hashtags,
        "media_type": media_type,
        "like_count": like_count,
        "hash_version": "v2",
        "metadata_hash": meta_hash,
    }
    append_live_capture_manifest_record(record, manifest_path=manifest_path)
    return record


def load_live_capture_manifest(manifest_path: Path | str | None = None) -> list[dict[str, Any]]:
    """Loads all records from an append-only JSONL capture manifest."""
    p = Path(manifest_path) if manifest_path else DEFAULT_LIVE_MANIFEST_PATH
    if not p.exists():
        return []
    records = []
    try:
        with open(p, "r", encoding="utf-8") as f:
            for line in f:
                line_str = line.strip()
                if line_str:
                    records.append(json.loads(line_str))
    except Exception as exc:
        logger.warning("Error reading capture manifest at %s: %s", p, exc)
    return records


def create_capture_manifest_record(
    sample: dict[str, Any],
    record_id: str | None = None,
    version: str = DEFAULT_METADATA_HASH_VERSION,
) -> dict[str, Any]:
    """Generates an independent capture manifest record from a captured sample.

    NOTE: This helper is provided for ingestion of real crawl events. It must NOT be used
    to retroactively fabricate provenance for unverified fixture samples.
    """
    plat = str(sample.get("platform") or "").strip().lower()
    vid = str(sample.get("platform_video_id") or "").strip()
    r_id = record_id or f"cap-{plat}-{vid}"
    raw_hashtags = sample.get("hashtags") or []
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
        "author": str(sample.get("author") or sample.get("author_name") or "").strip(),
        "title": str(sample.get("title") or "").strip(),
        "caption": str(sample.get("caption") or "").strip(),
        "hashtags": list(raw_hashtags),
        "media_type": str(sample.get("media_type") or "video").strip(),
        "like_count": sample.get("like_count"),
        "hash_version": version,
        "metadata_hash": compute_sample_metadata_hash(sample, version=version),
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
        if p.suffix == ".jsonl":
            return lookup_capture_manifest_record(load_live_capture_manifest(p), sample)
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
    annotation_manifest: Any = None,
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
            manifest_record_id=manifest_rec.get("record_id"),
        )

    # Validate manifest ID and canonical URL consistency
    m_vid = str(manifest_rec.get("platform_video_id") or manifest_rec.get("id") or "").strip()
    s_vid = str(sample.get("platform_video_id") or "").strip()
    if m_vid and s_vid and m_vid != s_vid:
        return SampleProvenanceResult(
            schema_valid=True,
            source_provenance_verified=False,
            status="UNVERIFIED_MANIFEST_MISMATCH",
            detail=f"Manifest video ID '{m_vid}' does not match sample video ID '{s_vid}'",
            reasons=[f"Manifest video ID mismatch: '{m_vid}' vs '{s_vid}'"],
            manifest_record_id=manifest_rec.get("record_id"),
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
            manifest_record_id=manifest_rec.get("record_id"),
        )

    clean_exp = str(expected_hash).strip().lower().removeprefix("sha256:")
    actual_hash_v2 = compute_sample_metadata_hash(sample, version="v2")
    clean_v2 = actual_hash_v2.strip().lower().removeprefix("sha256:")
    actual_hash_v1 = compute_sample_metadata_hash(sample, version="v1")
    clean_v1 = actual_hash_v1.strip().lower().removeprefix("sha256:")

    req_version = manifest_rec.get("hash_version", "v2")
    if req_version == "v1":
        hash_matched = (clean_exp == clean_v1)
        actual_hash_str = actual_hash_v1
    else:
        hash_matched = (clean_exp == clean_v2) or (clean_exp == clean_v1)
        actual_hash_str = actual_hash_v2 if clean_exp == clean_v2 else actual_hash_v1

    if not hash_matched:
        return SampleProvenanceResult(
            schema_valid=True,
            source_provenance_verified=False,
            status="UNVERIFIED_HASH_MISMATCH",
            detail=f"Metadata hash mismatch with manifest record '{manifest_rec.get('record_id')}': expected {expected_hash}, calculated {actual_hash_str}",
            reasons=[f"Metadata hash mismatch: expected {expected_hash}, got {actual_hash_str}"],
            manifest_record_id=manifest_rec.get("record_id"),
        )

    # Optional annotation verification
    ann_verified = False
    if annotation_manifest is not None:
        ann_rec = lookup_capture_manifest_record(annotation_manifest, sample)
        if ann_rec:
            ann_ok, ann_detail = audit_sample_annotation(sample, ann_rec)
            if not ann_ok:
                return SampleProvenanceResult(
                    schema_valid=True,
                    source_provenance_verified=False,
                    status="UNVERIFIED_ANNOTATION_MISMATCH",
                    detail=f"Annotation label verification failed: {ann_detail}",
                    reasons=[ann_detail],
                    annotation_verified=False,
                    manifest_record_id=manifest_rec.get("record_id"),
                )
            ann_verified = True
    elif sample.get("annotation_hash"):
        ann_ok, _ = audit_sample_annotation(sample)
        ann_verified = ann_ok

    return SampleProvenanceResult(
        schema_valid=True,
        source_provenance_verified=True,
        status="VERIFIED_STRICT",
        detail=f"Provenance strictly verified against capture manifest record '{manifest_rec.get('record_id')}'",
        reasons=[],
        annotation_verified=ann_verified,
        manifest_record_id=manifest_rec.get("record_id"),
    )
