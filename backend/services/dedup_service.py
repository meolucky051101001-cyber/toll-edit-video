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


class BulkDedupIndex:
    """Preloaded duplicate lookup for a batch of candidates.

    Three IN queries replace up to three per-candidate queries (a 200-result
    batch used to issue ~600 SELECTs). Videos inserted while the batch is
    processed must be registered via register() so in-batch duplicates are
    still detected — flushed-but-uncommitted rows never enter the preload.
    """

    def __init__(
        self,
        by_id: dict[tuple[str, str | None], Video],
        by_url: dict[str, Video],
        by_fp: dict[str, Video],
    ):
        self._by_id = by_id
        self._by_url = by_url
        self._by_fp = by_fp

    def register(self, video: Video) -> None:
        vid = video.platform_video_id
        if vid:
            self._by_id.setdefault((video.platform, vid), video)
        if video.canonical_url:
            self._by_url.setdefault(video.canonical_url, video)
        if video.fingerprint:
            self._by_fp.setdefault(video.fingerprint, video)


def build_dedup_index(db: Session, results: list[VideoResult]) -> BulkDedupIndex:
    ids = {r.platform_video_id for r in results if r.platform_video_id}
    urls = {canonical_url(str(r.url)) for r in results}
    fps = {fingerprint(r) for r in results}

    by_id: dict[tuple[str, str | None], Video] = {}
    if ids:
        for v in db.scalars(select(Video).where(Video.platform_video_id.in_(ids))):
            by_id.setdefault((v.platform, v.platform_video_id), v)

    by_url: dict[str, Video] = {}
    if urls:
        for v in db.scalars(select(Video).where(Video.canonical_url.in_(urls))):
            by_url.setdefault(v.canonical_url, v)

    by_fp: dict[str, Video] = {}
    if fps:
        for v in db.scalars(select(Video).where(Video.fingerprint.in_(fps))):
            by_fp.setdefault(v.fingerprint, v)

    return BulkDedupIndex(by_id=by_id, by_url=by_url, by_fp=by_fp)


def find_existing_preloaded(result: VideoResult, index: BulkDedupIndex) -> Video | None:
    """find_existing() semantics against a BulkDedupIndex instead of per-row SQL."""
    # 1. Match by platform and platform_video_id if available
    if result.platform_video_id:
        candidate = index._by_id.get((result.platform, result.platform_video_id))
        if candidate:
            return candidate

    # 2. Match by canonical URL
    candidate = index._by_url.get(canonical_url(str(result.url)))
    if candidate:
        if (
            result.platform_video_id
            and candidate.platform_video_id
            and result.platform_video_id.strip().lower() != candidate.platform_video_id.strip().lower()
        ):
            return None
        return candidate

    # 3. Fingerprint fallback for un-identified mock or partial items
    if (
        not result.platform_video_id
        and result.caption
        and (result.author_id or result.author_name or result.thumbnail_url)
    ):
        candidate = index._by_fp.get(fingerprint(result))
        if candidate and candidate.platform_video_id:
            return None
        return candidate

    return None
