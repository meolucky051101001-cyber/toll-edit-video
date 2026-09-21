from datetime import datetime, timezone
from uuid import uuid4

from sqlalchemy import DateTime, ForeignKey, Index, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from backend.core.database import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class VideoMetricSnapshot(Base):
    __tablename__ = "video_metric_snapshots"
    __table_args__ = (
        Index("ix_metric_snapshots_video_captured", "video_id", "captured_at"),
    )

    id: Mapped[str] = mapped_column(primary_key=True, default=lambda: str(uuid4()))
    video_id: Mapped[str] = mapped_column(
        ForeignKey("videos.id", ondelete="CASCADE"), index=True, nullable=False
    )
    like_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    comment_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    share_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    favorite_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    view_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    captured_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, index=True, nullable=False
    )
    source: Mapped[str] = mapped_column(String(50), default="search", nullable=False)
