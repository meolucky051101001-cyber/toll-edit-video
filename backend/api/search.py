import re

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from backend.core.config import settings
from backend.core.database import get_db
from backend.models import ImportBatch, SearchJob, SearchPlan
from backend.schemas.contracts import JobOut, SearchRequest
from backend.schemas.expansion import ExpandRequest, ExpansionOutcome
from backend.services.translator import plan_query_async

router = APIRouter(prefix="/api/search", tags=["search"])


@router.post("", response_model=JobOut, status_code=202)
async def create_search(payload: SearchRequest, request: Request, db: Session = Depends(get_db)):
    if len(request.app.state.search.tasks) >= 10:
        raise HTTPException(429, "Đã có 10 lượt tìm chờ xử lý. Vui lòng đợi hoặc hủy bớt.")
    use_mock = payload.mode == "mock" if payload.mode else settings.use_mock_provider
    if not use_mock:
        valid_platforms = {"xiaohongshu", "douyin"}
        if (
            len(payload.platforms) > 2
            or len(set(payload.platforms)) != len(payload.platforms)
            or not set(payload.platforms).issubset(valid_platforms)
            or payload.limit > 100
            or (payload.mode == "both" and set(payload.platforms) != valid_platforms)
            or (payload.mode in valid_platforms and payload.platforms != [payload.mode])
        ):
            raise HTTPException(
                422, "Tìm thật hỗ trợ Douyin, Xiaohongshu hoặc cả hai, tối đa 100 video/lượt. Các nền tảng khác chỉ có trong Dữ liệu mẫu."
            )
        if payload.selected_queries and len(payload.selected_queries) > 3:
            raise HTTPException(
                422, "Tìm thật chỉ dùng tối đa 3 truy vấn/lượt để hạn chế số trang cần mở."
            )
    job = SearchJob(
        original_query=payload.query,
        platforms=payload.platforms,
        requested_limit=payload.limit,
        min_relevance=payload.min_relevance if payload.min_relevance is not None else (40.0 if not use_mock else None),
        is_mock=use_mock,
    )
    db.add(job)
    db.flush()

    query_text = payload.query.strip()
    raw_tags = re.findall(r"#([^\s#]+)", query_text)
    has_tag_in_query = bool(raw_tags or query_text.startswith("#"))

    if payload.exact_hashtag and not has_tag_in_query and not payload.target_hashtag:
        raise HTTPException(
            400,
            "Khi bật chế độ khớp chính xác hashtag, truy vấn phải chứa thẻ hashtag (ví dụ: #decorbanhoc) hoặc cung cấp target_hashtag.",
        )

    target_hashtag = payload.target_hashtag or (
        raw_tags[0] if raw_tags else (query_text.lstrip("#").split()[0] if query_text.startswith("#") else None)
    )
    exact_hashtag = bool(payload.exact_hashtag or has_tag_in_query)

    db.add(
        SearchPlan(
            job_id=job.id,
            use_ai=payload.use_ai,
            queries=payload.selected_queries or [],
            source="manual" if payload.selected_queries else "original",
            filters={
                "minimum_likes": 0,
                "minimum_relevance": payload.min_relevance or 0.0,
                "sort_by": payload.sort_by,
                "publish_time": payload.publish_time,
                "content_type": payload.content_type,
                "exact_hashtag": exact_hashtag,
                "target_hashtag": target_hashtag,
            },
        )
    )
    db.commit()
    request.app.state.search.submit(job.id)
    return serialize_job(job, db)


@router.get("/jobs")
def list_jobs(page: int = Query(1, ge=1), db: Session = Depends(get_db)):
    jobs = db.scalars(
        select(SearchJob).order_by(SearchJob.created_at.desc()).offset((page - 1) * 20).limit(20)
    )
    return {
        "items": [serialize_job(j, db) for j in jobs],
        "total": db.scalar(select(func.count()).select_from(SearchJob)),
        "page": page,
    }


@router.get("/jobs/{job_id}", response_model=JobOut)
def get_job(job_id: str, db: Session = Depends(get_db)):
    job = db.get(SearchJob, job_id)
    if not job:
        raise HTTPException(404, "Không tìm thấy lượt tìm kiếm này.")
    return serialize_job(job, db)


@router.post("/jobs/{job_id}/cancel", response_model=JobOut)
async def cancel_job(job_id: str, request: Request, db: Session = Depends(get_db)):
    get_job(job_id, db)
    await request.app.state.search.cancel(job_id)
    db.expire_all()
    return get_job(job_id, db)


def serialize_job(job: SearchJob, db: Session) -> JobOut:
    result = JobOut.model_validate(job)
    if not result.active_platform and job.diagnostics and isinstance(job.diagnostics, dict):
        result.active_platform = job.diagnostics.get("active_platform")
    if not result.waiting_reason and job.diagnostics and isinstance(job.diagnostics, dict):
        result.waiting_reason = job.diagnostics.get("waiting_reason")
    if db.get(ImportBatch, job.id):
        result.import_source = "mediacrawler"
    plan = db.get(SearchPlan, job.id)
    if plan:
        result.queries = plan.queries
        result.ai_source = plan.source
        result.ai_warning = plan.warning
        result.query_expansion = plan.expansion
    else:
        result.queries = [job.original_query]
        result.ai_source = "original"
    return result


@router.post("/expand", response_model=ExpansionOutcome)
async def expand_query(payload: ExpandRequest, request: Request):
    if payload.use_ai and getattr(request.app.state, "ai", None):
        return await request.app.state.ai.expand(payload.query)
    plan = await plan_query_async(payload.query)
    return ExpansionOutcome(
        original_query=payload.query,
        source="dictionary",
        plan=plan,
        queries=plan.tiered_queries or [payload.query],
    )


@router.post("/jobs/{job_id}/resume", response_model=JobOut)
async def resume_job(job_id: str, request: Request, db: Session = Depends(get_db)):
    job = get_job(job_id, db)
    if job.status not in {
        "waiting_for_login",
        "waiting_for_user",
    }:
        raise HTTPException(
            409, "Lượt tìm này không chờ tiếp tục. Nếu ứng dụng đã khởi động lại, hãy tạo lượt mới."
        )

    can_resume, reason = await request.app.state.search.verify_and_resume(job_id)
    if not can_resume:
        raise HTTPException(409, detail=reason)

    db.expire_all()
    return get_job(job_id, db)

