from sqlalchemy import ForeignKey
from sqlalchemy.orm import Mapped, mapped_column

from backend.core.database import Base


class ImportBatch(Base):
    __tablename__ = "import_batches"
    job_id: Mapped[str] = mapped_column(
        ForeignKey("search_jobs.id", ondelete="CASCADE"), primary_key=True
    )
    source: Mapped[str] = mapped_column(default="mediacrawler")
    content_sha256: Mapped[str] = mapped_column(index=True)
    total_rows: Mapped[int]
    skipped_rows: Mapped[int]


class ImportOrigin(Base):
    __tablename__ = "import_origins"
    video_id: Mapped[str] = mapped_column(
        ForeignKey("videos.id", ondelete="CASCADE"), primary_key=True
    )
    batch_id: Mapped[str] = mapped_column(ForeignKey("import_batches.job_id"))
