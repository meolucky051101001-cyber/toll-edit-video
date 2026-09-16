from datetime import datetime
from uuid import uuid4

from sqlalchemy import JSON, DateTime, ForeignKey
from sqlalchemy.orm import Mapped, mapped_column

from backend.core.database import Base
from backend.models.video import utcnow


class SearchJob(Base):
    __tablename__ = "search_jobs"
    id: Mapped[str] = mapped_column(primary_key=True, default=lambda: str(uuid4()))
    original_query: Mapped[str]
    platforms: Mapped[list[str]] = mapped_column(JSON)
    status: Mapped[str] = mapped_column(default="pending", index=True)
    requested_limit: Mapped[int]
    found_count: Mapped[int] = mapped_column(default=0)
    processed_count: Mapped[int] = mapped_column(default=0)
    duplicate_count: Mapped[int] = mapped_column(default=0)
    provider_counts: Mapped[dict[str, int]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error_message: Mapped[str | None]
    is_mock: Mapped[bool] = mapped_column(default=True)


class SearchQuery(Base):
    __tablename__ = "search_queries"
    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    job_id: Mapped[str] = mapped_column(
        ForeignKey("search_jobs.id", ondelete="CASCADE"), index=True
    )
    platform: Mapped[str]
    query: Mapped[str]


class JobVideo(Base):
    __tablename__ = "job_videos"
    job_id: Mapped[str] = mapped_column(
        ForeignKey("search_jobs.id", ondelete="CASCADE"), primary_key=True
    )
    video_id: Mapped[str] = mapped_column(
        ForeignKey("videos.id", ondelete="CASCADE"), primary_key=True
    )
    relevance_score: Mapped[float]
    quality_score: Mapped[float]
    final_score: Mapped[float]
