import logging
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session
from starlette.middleware.trustedhost import TrustedHostMiddleware

from backend.api import browser, downloads, imports, search, settings, videos
from backend.core.config import settings as config
from backend.core.database import Base, SessionLocal, engine, get_db
from backend.core.logging import configure_logging
from backend.models import SearchJob, Video
from backend.providers.registry import get_provider
from backend.services.ai_service import AIService
from backend.services.browser_service import BrowserService
from backend.services.douyin_browser_service import DouyinBrowserService
from backend.services.download.browser_resolver import BrowserMediaResolver
from backend.services.download.service import DownloadService
from backend.services.search_service import SearchService


@asynccontextmanager
async def lifespan(app: FastAPI):
    configure_logging()
    Base.metadata.create_all(engine)
    try:
        with engine.connect() as conn:
            cols = [row[1] for row in conn.exec_driver_sql("PRAGMA table_info(videos)").fetchall()]
            if "script_analysis" not in cols and "id" in cols:
                conn.exec_driver_sql("ALTER TABLE videos ADD COLUMN script_analysis JSON")
                conn.commit()
    except Exception:
        pass
    app.state.browser = BrowserService()
    app.state.douyin_browser = DouyinBrowserService()
    app.state.ai = AIService(config)
    app.state.search = SearchService(
        SessionLocal,
        factory=lambda platform, mock: get_provider(
            platform, mock, app.state.browser, app.state.douyin_browser
        ),
        ai=app.state.ai,
    )
    app.state.search.recover_interrupted()
    app.state.download = DownloadService(
        SessionLocal,
        browser_resolver=BrowserMediaResolver(
            xhs_browser_service=app.state.browser,
            douyin_browser_service=app.state.douyin_browser,
        ),
    )
    app.state.download.recover_interrupted_jobs()
    yield
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
async def local_origin(request: Request, call_next):
    origin = request.headers.get("origin")
    if (
        request.method not in {"GET", "HEAD", "OPTIONS"}
        and origin
        and origin
        not in {
            "http://localhost:3000",
            "http://127.0.0.1:3000",
            "http://127.0.0.1:8000",
            "http://localhost:8000",
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
app.include_router(settings.router)

app.include_router(imports.router)

app.include_router(browser.router)
app.include_router(browser.douyin_router)
