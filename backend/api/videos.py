from datetime import datetime
from typing import Literal

from fastapi import APIRouter, Body, Depends, HTTPException, Query, Request
from sqlalchemy import String, cast, func, or_, select
from sqlalchemy.orm import Session

from backend.core.database import get_db
from backend.models import ImportOrigin, JobVideo, Video
from backend.schemas.contracts import Platform, VideoOut, VideoPatch, VideoStatus
from backend.schemas.script_analysis import ScriptAnalysisOut, ScriptAnalysisRequest

router = APIRouter(prefix="/api/videos", tags=["videos"])


@router.get("")
def list_videos(
    job_id: str | None = None,
    platform: Platform | None = None,
    status: VideoStatus | None = None,
    q: str = Query("", max_length=300),
    min_relevance: float = Query(0, ge=0, le=100),
    min_likes: int = Query(0, ge=0),
    since: datetime | None = None,
    min_duration: int | None = Query(None, ge=0),
    max_duration: int | None = Query(None, ge=0),
    sort: Literal["best", "likes", "newest", "comments", "favorites"] = "best",
    page: int = Query(1, ge=1),
    page_size: int = Query(24, ge=1, le=100),
    db: Session = Depends(get_db),
):
    statement = select(Video)
    score = Video.final_score
    relevance = Video.relevance_score
    if job_id:
        statement = statement.join(JobVideo).where(JobVideo.job_id == job_id)
        score, relevance = JobVideo.final_score, JobVideo.relevance_score
    if platform:
        statement = statement.where(Video.platform == platform)
    if status:
        statement = statement.where(Video.status == status)
    if q.strip():
        pattern = (
            "%" + q.strip().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
        )
        statement = statement.where(
            or_(
                *(
                    column.ilike(pattern, escape="\\")
                    for column in (
                        Video.caption,
                        Video.title,
                        Video.author_name,
                        cast(Video.hashtags, String),
                        Video.search_query,
                    )
                )
            )
        )
    if since:
        statement = statement.where(Video.published_at >= since)
    if min_duration is not None:
        statement = statement.where(Video.duration_seconds >= min_duration)
    if max_duration is not None:
        statement = statement.where(Video.duration_seconds <= max_duration)
    statement = statement.where(relevance >= min_relevance)
    if min_likes:
        statement = statement.where(Video.like_count >= min_likes)
    total = db.scalar(select(func.count()).select_from(statement.subquery()))
    order = {
        "best": score,
        "likes": Video.like_count,
        "newest": Video.published_at,
        "comments": Video.comment_count,
        "favorites": Video.favorite_count,
    }[sort]
    rows = db.scalars(
        statement.order_by(order.desc().nulls_last(), Video.id)
        .offset((page - 1) * page_size)
        .limit(page_size)
    )
    items = []
    for row in rows:
        item = video_output(row, db)
        if job_id:
            link = db.get(JobVideo, (job_id, row.id))
            if link:
                item = item.update_validated(
                    relevance_score=link.relevance_score,
                    quality_score=link.quality_score,
                    final_score=link.final_score,
                )
        items.append(item)
    return {"items": items, "total": total, "page": page, "page_size": page_size}


@router.get("/{video_id}", response_model=VideoOut)
def get_video(video_id: str, db: Session = Depends(get_db)):
    video = db.get(Video, video_id)
    if not video:
        raise HTTPException(404, "Không tìm thấy video này.")
    return video_output(video, db)


@router.patch("/{video_id}", response_model=VideoOut)
def patch_video(video_id: str, payload: VideoPatch, db: Session = Depends(get_db)):
    video = db.get(Video, video_id)
    if not video:
        raise HTTPException(404, "Không tìm thấy video này.")
    video.status = payload.status
    db.commit()
    return video_output(video, db)


@router.post("/{video_id}/save", response_model=VideoOut)
def save_video(video_id: str, db: Session = Depends(get_db)):
    return patch_video(video_id, VideoPatch(status="saved"), db)


@router.post("/{video_id}/skip", response_model=VideoOut)
def skip_video(video_id: str, db: Session = Depends(get_db)):
    return patch_video(video_id, VideoPatch(status="skipped"), db)


@router.post("/{video_id}/script-analysis", response_model=ScriptAnalysisOut)
async def analyze_video_script(
    video_id: str,
    request: Request,
    payload: ScriptAnalysisRequest = Body(default_factory=ScriptAnalysisRequest),
    db: Session = Depends(get_db),
):
    video = db.get(Video, video_id)
    if not video:
        raise HTTPException(404, "Không tìm thấy video này.")

    if not payload.force_refresh and video.script_analysis:
        try:
            cached_data = dict(video.script_analysis)
            cached_data["cached"] = True
            return ScriptAnalysisOut.model_validate(cached_data)
        except Exception:
            pass

    ai_service = getattr(request.app.state, "ai", None) if request else None
    video_dict = {
        "title": video.title,
        "caption": video.caption,
        "hashtags": video.hashtags or [],
        "duration_seconds": video.duration_seconds,
        "platform": video.platform,
        "like_count": video.like_count,
        "comment_count": video.comment_count,
        "share_count": video.share_count,
    }

    if ai_service:
        analysis = await ai_service.analyze_script(video_id, video_dict)
    else:
        from backend.services.ai_service import generate_fallback_script_analysis

        analysis = generate_fallback_script_analysis(video_id, video_dict)

    video.script_analysis = analysis.model_dump()
    db.commit()
    return analysis


def video_output(video: Video, db: Session) -> VideoOut:
    result = VideoOut.model_validate(video)
    if db.get(ImportOrigin, video.id):
        return result.update_validated(imported_from="mediacrawler")
    return result
