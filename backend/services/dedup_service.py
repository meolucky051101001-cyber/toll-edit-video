import hashlib
import json
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.models import Video
from backend.schemas.contracts import VideoResult

TRACKING_PARAMS = {
    "utm_source",
    "utm_medium",
    "utm_campaign",
    "utm_content",
    "utm_term",
    "share_token",
    "xsec_token",
    "xsec_source",
    "from",
    "source",
}


def canonical_url(url: str) -> str:
    parts = urlsplit(url)
    query = urlencode(
        sorted((k, v) for k, v in parse_qsl(parts.query) if k.lower() not in TRACKING_PARAMS)
    )
    return urlunsplit(
        (parts.scheme.lower(), parts.netloc.lower(), parts.path.rstrip("/"), query, "")
    )


def fingerprint(result: VideoResult) -> str:
    payload = [
        result.platform,
        result.author_id or result.author_name,
        " ".join((result.caption or "").lower().split()),
        result.thumbnail_url,
    ]
    return hashlib.sha256(json.dumps(payload, ensure_ascii=False).encode()).hexdigest()


def find_existing(db: Session, result: VideoResult) -> Video | None:
    # 1. Match by platform and platform_video_id if available
    if result.platform_video_id:
        candidate = db.scalar(
            select(Video)
            .where(
                Video.platform == result.platform,
                Video.platform_video_id == result.platform_video_id,
            )
            .limit(1)
        )
        if candidate:
            return candidate

    # 2. Match by canonical URL
    canon = canonical_url(str(result.url))
    candidate = db.scalar(select(Video).where(Video.canonical_url == canon).limit(1))
    if candidate:
        if (
            result.platform_video_id
            and candidate.platform_video_id
            and result.platform_video_id.strip().lower() != candidate.platform_video_id.strip().lower()
        ):
            return None
        return candidate

    # 3. Fallback fingerprint for un-identified mock or partial items
    if (
        not result.platform_video_id
        and result.caption
        and (result.author_id or result.author_name or result.thumbnail_url)
    ):
        candidate = db.scalar(select(Video).where(Video.fingerprint == fingerprint(result)).limit(1))
        if candidate and candidate.platform_video_id:
            return None
        return candidate

    return None
