from sqlalchemy import JSON, ForeignKey
from sqlalchemy.orm import Mapped, mapped_column

from backend.core.database import Base


class SearchPlan(Base):
    """Additive table: existing jobs remain readable without column migrations."""

    __tablename__ = "search_plans"
    job_id: Mapped[str] = mapped_column(
        ForeignKey("search_jobs.id", ondelete="CASCADE"), primary_key=True
    )
    use_ai: Mapped[bool] = mapped_column(default=False)
    queries: Mapped[list[str]] = mapped_column(JSON, default=list)
    expansion: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    source: Mapped[str] = mapped_column(default="original")
    warning: Mapped[str | None]
