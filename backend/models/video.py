from datetime import datetime, timezone
from uuid import uuid4

from sqlalchemy import JSON, DateTime, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from backend.core.database import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Video(Base):
    __tablename__ = "videos"
    __table_args__ = (UniqueConstraint("platform", "platform_video_id"),)
    id: Mapped[str] = mapped_column(primary_key=True, default=lambda: str(uuid4()))
    platform: Mapped[str] = mapped_column(index=True)
    platform_video_id: Mapped[str | None]
    url: Mapped[str] = mapped_column(unique=True)
    canonical_url: Mapped[str] = mapped_column(unique=True)
    fingerprint: Mapped[str] = mapped_column(index=True)
    share_url: Mapped[str | None]
    thumbnail_url: Mapped[str | None]
    title: Mapped[str | None]
    caption: Mapped[str | None]
    author_name: Mapped[str | None]
    author_id: Mapped[str | None]
    author_url: Mapped[str | None]
    hashtags: Mapped[list[str]] = mapped_column(JSON, default=list)
    keywords: Mapped[list[str]] = mapped_column(JSON, default=list)
    duration_seconds: Mapped[float | None]
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    like_count: Mapped[int | None]
    comment_count: Mapped[int | None]
    share_count: Mapped[int | None]
    favorite_count: Mapped[int | None]
    view_count: Mapped[int | None]
    search_query: Mapped[str]
    search_job_id: Mapped[str]
    relevance_score: Mapped[float] = mapped_column(default=0)
    quality_score: Mapped[float] = mapped_column(default=0)
    final_score: Mapped[float] = mapped_column(default=0)
    status: Mapped[str] = mapped_column(String(20), default="new", index=True)
    is_mock: Mapped[bool] = mapped_column(default=False)
    script_analysis: Mapped[dict | None] = mapped_column(JSON, default=None, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )
