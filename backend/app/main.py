import logging
import time
from contextlib import asynccontextmanager
from typing import Any

from fastapi import Depends, FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session
from starlette.middleware.trustedhost import TrustedHostMiddleware

from backend.api import (
    browser,
    downloads,
    dubbing,
    events,
    imports,
    observability,
    platforms,
    search,
    settings,
    system,
    videos,
)
from backend.core.config import settings as config
from backend.core.database import Base, SessionLocal, engine, get_db
from backend.core.events import event_bus
from backend.core.logging import configure_logging
from backend.models import SearchJob, Video
from backend.models.dubbing_job import DubbingJob
from backend.providers.registry import get_provider
from backend.services.ai_service import AIService
from backend.services.browser_service import BrowserService
from backend.services.douyin_browser_service import DouyinBrowserService
from backend.services.download.browser_resolver import BrowserMediaResolver
from backend.services.download.service import DownloadService
from backend.services.metric_service import MetricService
from backend.services.observability import telemetry_service
from backend.services.search_service import SearchService
from backend.services.virality_service import ViralityService


@asynccontextmanager
async def lifespan(app: FastAPI):
    configure_logging()
    Base.metadata.create_all(engine)
    try:
        with engine.connect() as conn:
            cols = [row[1] for row in conn.exec_driver_sql("PRAGMA table_info(videos)").fetchall()]
            if "script_analysis" not in cols and "id" in cols:
                conn.exec_driver_sql("ALTER TABLE videos ADD COLUMN script_analysis JSON")
            if "velocity_score" not in cols and "id" in cols:
                conn.exec_driver_sql("ALTER TABLE videos ADD COLUMN velocity_score FLOAT DEFAULT 0.0")
            if "virality_score" not in cols and "id" in cols:
                conn.exec_driver_sql("ALTER TABLE videos ADD COLUMN virality_score FLOAT DEFAULT 0.0")

            jv_cols = [row[1] for row in conn.exec_driver_sql("PRAGMA table_info(job_videos)").fetchall()]
            if "velocity_score" not in jv_cols and "job_id" in jv_cols:
                conn.exec_driver_sql("ALTER TABLE job_videos ADD COLUMN velocity_score FLOAT DEFAULT 0.0")
            if "virality_score" not in jv_cols and "job_id" in jv_cols:
                conn.exec_driver_sql("ALTER TABLE job_videos ADD COLUMN virality_score FLOAT DEFAULT 0.0")

            sj_cols = [row[1] for row in conn.exec_driver_sql("PRAGMA table_info(search_jobs)").fetchall()]
            if "rejected_count" not in sj_cols and "id" in sj_cols:
                conn.exec_driver_sql("ALTER TABLE search_jobs ADD COLUMN rejected_count INTEGER DEFAULT 0")
            if "min_relevance" not in sj_cols and "id" in sj_cols:
                conn.exec_driver_sql("ALTER TABLE search_jobs ADD COLUMN min_relevance FLOAT")
            if "diagnostics" not in sj_cols and "id" in sj_cols:
                conn.exec_driver_sql("ALTER TABLE search_jobs ADD COLUMN diagnostics JSON")
            if "actual_ai_model" not in sj_cols and "id" in sj_cols:
                conn.exec_driver_sql("ALTER TABLE search_jobs ADD COLUMN actual_ai_model VARCHAR")
            if "ai_fallback_occurred" not in sj_cols and "id" in sj_cols:
                conn.exec_driver_sql("ALTER TABLE search_jobs ADD COLUMN ai_fallback_occurred BOOLEAN DEFAULT 0")
            if "ai_latency_ms" not in sj_cols and "id" in sj_cols:
                conn.exec_driver_sql("ALTER TABLE search_jobs ADD COLUMN ai_latency_ms FLOAT")
            conn.commit()
    except Exception:
        pass
    app.state.event_bus = event_bus
    app.state.metrics = MetricService(
        SessionLocal,
        event_bus=app.state.event_bus,
        throttle_seconds=config.metric_snapshot_throttle_seconds,
    )
    app.state.virality = ViralityService(event_bus=app.state.event_bus)
    app.state.browser = BrowserService(
        headless=config.playwright_headless,
        hybrid_headless=config.hybrid_headless,
    )
    app.state.douyin_browser = DouyinBrowserService(
        headless=config.playwright_headless,
        hybrid_headless=config.hybrid_headless,
    )
    app.state.ai = AIService(config)
    app.state.search = SearchService(
        SessionLocal,
        factory=lambda platform, mock: get_provider(
            platform,
            mock,
            app.state.browser,
            app.state.douyin_browser,
            auto_elevate=False if getattr(config, "search_headless_silent", True) else config.browser_auto_elevate,
        ),
        ai=app.state.ai,
        event_bus=app.state.event_bus,
        metric_service=app.state.metrics,
    )
    app.state.search.recover_interrupted()
    app.state.download = DownloadService(
        SessionLocal,
        browser_resolver=BrowserMediaResolver(
            xhs_browser_service=app.state.browser,
            douyin_browser_service=app.state.douyin_browser,
        ),
        event_bus=app.state.event_bus,
    )
    app.state.download.recover_interrupted_jobs()
    await app.state.download.start()

    from backend.core.queue import system_worker_pool

    async def handle_dubbing(payload: dict[str, Any]) -> None:
        job_id = payload.get("job_id")
        if job_id:
            from backend.api.dubbing import dubbing_service
            with SessionLocal() as db:
                job = db.get(DubbingJob, job_id)
                if not job or job.status in ("completed", "failed"):
                    return
            await dubbing_service.process_dubbing(job_id)

    async def handle_download(payload: dict[str, Any]) -> None:
        job_id = payload.get("job_id")
        if job_id and hasattr(app.state, "download"):
            await app.state.download.process_download(job_id)

    async def handle_search(payload: dict[str, Any]) -> None:
        job_id = payload.get("job_id")
        if job_id and hasattr(app.state, "search"):
            await app.state.search.run(job_id)

    system_worker_pool.register_handler("dubbing", handle_dubbing)
    system_worker_pool.register_handler("download", handle_download)
    system_worker_pool.register_handler("search", handle_search)

    await system_worker_pool.start()
    yield
    await system_worker_pool.stop()
    await app.state.download.shutdown()
    await app.state.search.shutdown()
    await app.state.browser.close()
    await app.state.douyin_browser.close()
    engine.dispose()


app = FastAPI(title="AI Video Research Tool", version="0.2.0", lifespan=lifespan)
app.add_middleware(TrustedHostMiddleware, allowed_hosts=["localhost", "127.0.0.1", "testserver"])
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3000", "http://127.0.0.1:3000"],
    allow_methods=["GET", "POST", "PATCH"],
    allow_headers=["Content-Type"],
)


@app.middleware("http")
async def telemetry_middleware(request: Request, call_next):
    start_time = time.perf_counter()
    status_code = 500
    try:
        response = await call_next(request)
        status_code = response.status_code
        return response
    except Exception:
        status_code = 500
        raise
    finally:
        duration_ms = (time.perf_counter() - start_time) * 1000.0
        telemetry_service.record_counter("http_requests_total", 1)
        telemetry_service.record_latency("http_request_ms", duration_ms)
        if status_code >= 400:
            telemetry_service.record_counter("http_errors_total", 1)


@app.middleware("http")
async def local_origin(request: Request, call_next):
    origin = request.headers.get("origin")
    if (
        request.method not in {"GET", "HEAD", "OPTIONS"}
        and origin
        and origin
        not in {
            "http://localhost:3000",
            "http://127.0.0.1:3000",
            "http://127.0.0.1:8010",
            "http://localhost:8010",
        }
    ):
        return JSONResponse(
            status_code=403, content={"detail": "Chỉ nhận thao tác từ ứng dụng local."}
        )
    return await call_next(request)


@app.exception_handler(RequestValidationError)
async def validation_error(request: Request, error: RequestValidationError):
    return JSONResponse(
        status_code=422,
        content={
            "detail": "Dữ liệu không hợp lệ. Kiểm tra chủ đề, nền tảng và giới hạn 1–200 kết quả."
        },
    )


@app.exception_handler(Exception)
async def unexpected_error(request: Request, error: Exception):
    logging.getLogger("research").error(
        "request_path=%s error_type=%s", request.url.path, type(error).__name__
    )
    return JSONResponse(
        status_code=500, content={"detail": "Ứng dụng gặp lỗi. Xem logs/app.log rồi thử lại."}
    )


@app.get("/api/health")
def health(db: Session = Depends(get_db)):
    db.execute(text("SELECT 1"))
    return {
        "status": "online",
        "app": "ai-video-research-tool",
        "database": "online",
        "mode": "mock" if config.use_mock_provider else "real",
    }


@app.get("/api/stats")
def stats(db: Session = Depends(get_db)):
    return {
        "total": db.scalar(select(func.count()).select_from(Video)),
        "saved": db.scalar(select(func.count()).select_from(Video).where(Video.status == "saved")),
        "used": db.scalar(select(func.count()).select_from(Video).where(Video.status == "used")),
        "jobs": db.scalar(select(func.count()).select_from(SearchJob)),
    }


app.include_router(search.router)
app.include_router(videos.router)
app.include_router(downloads.router)
app.include_router(events.router)
app.include_router(settings.router)
app.include_router(imports.router)
app.include_router(dubbing.router)
app.include_router(platforms.router)
app.include_router(system.router)
app.include_router(observability.router)

app.include_router(browser.router)
app.include_router(browser.douyin_router)
