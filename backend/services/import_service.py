from sqlalchemy.orm import Session

from backend.importers.mediacrawler import ParsedImport
from backend.models import ImportBatch, ImportOrigin, JobVideo, SearchJob, Video
from backend.models.video import utcnow
from backend.services.dedup_service import canonical_url, find_existing, fingerprint
from backend.services.ranking_service import rank


def preview(parsed: ParsedImport, db: Session) -> dict:
    existing = sum(find_existing(db, item.video) is not None for item in parsed.items)
    return {
        "total": parsed.total,
        "accepted": len(parsed.items),
        "existing": existing,
        "new": len(parsed.items) - existing,
        "skipped": len(parsed.issues),
        "issues": [issue.model_dump() for issue in parsed.issues],
        "sample": [
            {
                "title": item.video.title,
                "platform": item.video.platform,
                "like_count": item.video.like_count,
                "published_at": item.video.published_at,
            }
            for item in parsed.items[:8]
        ],
    }


def commit_import(parsed: ParsedImport, db: Session) -> dict:
    if not parsed.items:
        raise ValueError("Không có video hợp lệ để nhập.")
    summary = preview(parsed, db)
    job = SearchJob(
        original_query="Nhập JSON MediaCrawler",
        platforms=list(dict.fromkeys(item.video.platform for item in parsed.items)),
        requested_limit=len(parsed.items),
        is_mock=False,
        status="completed",
        started_at=utcnow(),
        completed_at=utcnow(),
        found_count=parsed.total,
        processed_count=len(parsed.items),
        duplicate_count=summary["existing"],
        provider_counts={},
    )
    db.add(job)
    db.flush()
    db.add(
        ImportBatch(
            job_id=job.id,
            content_sha256=parsed.digest,
            total_rows=parsed.total,
            skipped_rows=len(parsed.issues),
        )
    )
    db.flush()
    counts: dict[str, int] = {}
    for item in parsed.items:
        result = item.video
        video = find_existing(db, result)
        scores = rank(result, item.query)
        if video is None:
            values = result.model_dump(mode="python")
            values["url"] = str(result.url)
            video = Video(
                **values,
                canonical_url=canonical_url(str(result.url)),
                fingerprint=fingerprint(result),
                search_query=item.query,
                search_job_id=job.id,
                relevance_score=scores[0],
                quality_score=scores[1],
                final_score=scores[2],
            )
            db.add(video)
            db.flush()
            db.add(ImportOrigin(video_id=video.id, batch_id=job.id))
        # Imported snapshots do not overwrite existing metadata or user's status.
        db.add(
            JobVideo(
                job_id=job.id,
                video_id=video.id,
                relevance_score=scores[0],
                quality_score=scores[1],
                final_score=scores[2],
            )
        )
        counts[result.platform] = counts.get(result.platform, 0) + 1
    job.provider_counts = counts
    db.commit()
    return {**summary, "job_id": job.id, "imported": len(parsed.items)}
