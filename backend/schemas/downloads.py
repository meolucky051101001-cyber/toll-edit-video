from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from backend.schemas.contracts import Platform

DownloadJobStatus = Literal[
    "queued",
    "resolving",
    "downloading",
    "completed",
    "failed",
    "cancelled",
    "interrupted",
]


class ResolvedMedia(BaseModel):
    model_config = ConfigDict(frozen=True)

    platform: Platform
    video_id: str
    media_url: str
    headers: dict[str, str] = Field(default_factory=dict)
    format: str = "mp4"
    quality: str | None = None
    adapter_name: str
    adapter_version: str


class DownloadJobOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    video_id: str
    platform: Platform
    status: DownloadJobStatus
    bytes_downloaded: int = 0
    total_bytes: int | None = None
    progress_percent: float | None = None
    file_size: int | None = None
    error_code: str | None = None
    error_message: str | None = None
    adapter_version: str | None = None
    created_at: datetime
    updated_at: datetime


class BatchDownloadRequest(BaseModel):
    video_ids: list[str] = Field(..., min_length=1, max_length=100)
    force: bool = False
