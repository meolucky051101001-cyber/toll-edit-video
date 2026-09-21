from datetime import datetime, timezone
from uuid import uuid4

from sqlalchemy import Boolean, DateTime, Float, ForeignKey, Index, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from backend.core.database import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class DubbingJob(Base):
    __tablename__ = "dubbing_jobs"
    __table_args__ = (
        Index("ix_dubbing_jobs_video_created", "video_id", "created_at"),
    )

    id: Mapped[str] = mapped_column(primary_key=True, default=lambda: str(uuid4()))
    video_id: Mapped[str] = mapped_column(
        ForeignKey("videos.id", ondelete="CASCADE"), index=True, nullable=False
    )
    status: Mapped[str] = mapped_column(String(50), default="pending", nullable=False)
    voice_id: Mapped[str] = mapped_column(String(100), default="vi-VN-HoaiMyNeural", nullable=False)
    script_text: Mapped[str] = mapped_column(String, nullable=False)
    speed: Mapped[float] = mapped_column(Float, default=1.0, nullable=False)
    pitch: Mapped[float] = mapped_column(Float, default=1.0, nullable=False)
    bgm_volume_percent: Mapped[int] = mapped_column(Integer, default=15, nullable=False)
    ducking: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    audio_path: Mapped[str | None] = mapped_column(String, nullable=True)
    video_path: Mapped[str | None] = mapped_column(String, nullable=True)
    duration_seconds: Mapped[float | None] = mapped_column(Float, nullable=True)
    error_message: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, index=True, nullable=False
    )
    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
