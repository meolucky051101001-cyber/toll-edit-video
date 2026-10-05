from collections.abc import Mapping
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Literal
from urllib.parse import urlparse

try:
    from typing import Self
except ImportError:
    from typing_extensions import Self

from pydantic import BaseModel, ConfigDict, Field, HttpUrl, field_validator, model_validator

Platform = Literal["douyin", "xiaohongshu", "tiktok", "youtube_shorts", "mock"]


class PlatformCapabilities(BaseModel):
    id: str
    name: str
    display_name: str
    supports_headless: bool = True
    supports_video_download: bool = True
    supports_metrics: bool = True
    supports_hashtags: bool = True
    requires_login_for_search: bool = False
    auth_domains: list[str] = Field(default_factory=list)
    icon_name: str = "video"
    is_enabled: bool = True
    description: str = ""

VideoStatus = Literal["new", "saved", "skipped", "used", "favorite"]
JobStatus = Literal[
    "pending",
    "expanding_query",
    "searching",
    "ranking",
    "waiting_for_login",
    "waiting_for_user",
    "resuming",
    "completed",
    "interrupted",
    "failed",
    "cancelled",
]
VALID_JOB_STATUSES: set[str] = {
    "pending",
    "expanding_query",
    "searching",
    "ranking",
    "waiting_for_login",
    "waiting_for_user",
    "resuming",
    "completed",
    "interrupted",
    "failed",
    "cancelled",
}


class SearchRequest(BaseModel):
    mode: Literal["mock", "xiaohongshu", "douyin", "both", "tiktok", "youtube_shorts"] | None = None
    use_ai: bool = False
    selected_queries: list[str] | None = Field(default=None, min_length=1, max_length=10)
    query: str = Field(min_length=1, max_length=300)
    platforms: list[Platform] = Field(default=["douyin", "xiaohongshu"], min_length=1, max_length=5)
    limit: int = Field(default=50, ge=1, le=200)
    min_relevance: float | None = Field(default=None, ge=0.0, le=100.0)
    sort_by: Literal["relevance", "most_liked", "newest"] = "relevance"
    publish_time: Literal["all", "day", "week", "six_months"] = "all"
    content_type: Literal["all", "video", "image_text"] = "all"
    exact_hashtag: bool = False
    target_hashtag: str | None = None

    @field_validator("selected_queries")
    @classmethod
    def validate_selected_queries(cls, value: list[str] | None) -> list[str] | None:
        if value is None:
            return None
        cleaned = [" ".join(term.split()) for term in value]
        if any(not term or len(term) > 300 for term in cleaned):
            raise ValueError("Invalid selected keyword.")
        return list(dict.fromkeys(cleaned))

    @field_validator("query")
    @classmethod
    def clean_query(cls, value: str) -> str:
        value = " ".join(value.split())
        if not value:
            raise ValueError("Vui lòng nhập chủ đề tìm kiếm.")
        return value

    @field_validator("platforms")
    @classmethod
    def unique_platforms(cls, value: list[Platform]) -> list[Platform]:
        return list(dict.fromkeys(value))


class SearchFilters(BaseModel):
    minimum_likes: int = 0
    minimum_relevance: float = 0.0
    sort_by: Literal["relevance", "most_liked", "newest"] = "relevance"
    publish_time: Literal["all", "day", "week", "six_months"] = "all"
    content_type: Literal["all", "video", "image_text"] = "all"
    exact_hashtag: bool = False
    target_hashtag: str | None = None


def validate_url_policy(
    url_str: str,
    platform: Platform,
    is_mock: bool,
    field_name: str,
) -> None:
    try:
        parsed = urlparse(url_str)
    except Exception:
        raise ValueError(f"Invalid {field_name}.")
    if parsed.scheme != "https":
        raise ValueError(f"Insecure {field_name} protocol '{parsed.scheme}': must be https.")
    if parsed.username or parsed.password:
        raise ValueError(f"Credentials not allowed in {field_name}.")
    if parsed.port is not None and parsed.port != 443:
        raise ValueError(f"Custom port '{parsed.port}' not allowed in {field_name}.")
    host = (parsed.hostname or "").lower()
    if not host:
        raise ValueError(f"Missing host in {field_name}.")

    is_invalid_mock_host = host == "example.invalid" or host.endswith(".example.invalid")

    # Strictly forbid example.invalid for non-mock results
    if not is_mock and is_invalid_mock_host:
        raise ValueError(f"Non-mock {field_name} host '{host}' cannot be example.invalid.")

    trusted: tuple[str, ...]
    if platform == "xiaohongshu":
        trusted = ("xiaohongshu.com", "rednote.com", "xhslink.com")
    elif platform == "douyin":
        trusted = ("douyin.com", "iesdouyin.com")
    elif platform == "tiktok":
        trusted = ("tiktok.com", "vm.tiktok.com")
    elif platform == "youtube_shorts":
        trusted = ("youtube.com", "youtu.be")
    else:
        trusted = (
            "xiaohongshu.com",
            "rednote.com",
            "xhslink.com",
            "douyin.com",
            "iesdouyin.com",
            "tiktok.com",
            "vm.tiktok.com",
            "youtube.com",
            "youtu.be",
        )

    is_trusted_platform_host = any(host == h or host.endswith("." + h) for h in trusted)

    # For mock results: allow example.invalid OR trusted platform hosts
    if is_mock:
        if not (is_invalid_mock_host or is_trusted_platform_host):
            raise ValueError(
                f"Mock {field_name} host '{host}' must be example.invalid or trusted platform host."
            )
        return

    # For non-mock results: must be trusted platform host
    if not is_trusted_platform_host:
        raise ValueError(f"Untrusted {field_name} host '{host}' for platform '{platform}'.")


class VideoResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    platform: Platform
    platform_video_id: str | None = None
    url: HttpUrl
    share_url: str | None = None
    thumbnail_url: str | None = None
    title: str | None = None
    caption: str | None = None
    author_name: str | None = None
    author_id: str | None = None
    author_url: str | None = None
    hashtags: list[str] = Field(default_factory=list)
    keywords: list[str] = Field(default_factory=list)
    duration_seconds: float | None = Field(None, ge=0)
    published_at: datetime | None = None
    like_count: int | None = Field(None, ge=0)
    comment_count: int | None = Field(None, ge=0)
    share_count: int | None = Field(None, ge=0)
    favorite_count: int | None = Field(None, ge=0)
    view_count: int | None = Field(None, ge=0)
    media_type: Literal["video", "image_text"] = "video"
    images: list[str] = Field(default_factory=list)
    velocity_score: float | None = None
    is_mock: bool = False

    @model_validator(mode="before")
    @classmethod
    def infer_mock_and_identity(cls, data: Any) -> Any:
        if isinstance(data, dict):
            if "favorite_count" not in data and "collect_count" in data:
                data = dict(data)
                data["favorite_count"] = data["collect_count"]
            raw_url = str(data.get("url") or "")
            if "is_mock" not in data or data["is_mock"] is None:
                try:
                    host = (urlparse(raw_url).hostname or "").lower()
                    if host == "example.invalid" or host.endswith(".example.invalid"):
                        data = dict(data)
                        data["is_mock"] = True
                except Exception:
                    pass

            # Infer missing platform_video_id from url
            platform = str(data.get("platform") or "")
            pvid = data.get("platform_video_id")
            if not pvid and raw_url and platform:
                from backend.services.identity_policy import extract_platform_id
                inferred = extract_platform_id(platform, raw_url)
                if inferred:
                    data = dict(data)
                    data["platform_video_id"] = inferred
        return data

    @model_validator(mode="after")
    def validate_urls(self) -> Self:
        validate_url_policy(str(self.url), self.platform, self.is_mock, "url")
        if self.share_url is not None:
            validate_url_policy(self.share_url, self.platform, self.is_mock, "share_url")
        from backend.services.identity_policy import validate_result_identity
        is_valid, _, error_msg = validate_result_identity(
            self.platform,
            self.platform_video_id,
            str(self.url),
            self.share_url,
            is_mock=self.is_mock,
        )
        if not is_valid:
            raise ValueError(error_msg or "Mâu thuẫn danh tính giữa platform_video_id và URL.")
        return self

    def update_validated(self, **updates: object) -> Self:
        """Creates a new instance with validated updates, preserving immutability."""
        data = self.model_dump(mode="python")
        data.update(updates)
        return self.__class__.model_validate(data)

    def model_copy(
        self,
        *,
        update: Mapping[str, Any] | None = None,
        deep: bool = False,
    ) -> Self:
        """Overridden to ensure any updates pass through strict model validation."""
        if update:
            return self.update_validated(**dict(update))
        return super().model_copy(deep=deep)

    @field_validator("published_at")
    @classmethod
    def published_utc(cls, value: datetime | None) -> datetime | None:
        return value.replace(tzinfo=timezone.utc) if value and value.tzinfo is None else value

    @field_validator("images", mode="before")
    @classmethod
    def normalize_images(cls, value: Any) -> list[str]:
        if value is None:
            return []
        if isinstance(value, list):
            return [str(v) for v in value if v]
        return []

    @field_validator("media_type", mode="before")
    @classmethod
    def normalize_media_type(cls, value: Any) -> str:
        return value or "video"


def update_video_result(video: VideoResult, **updates: object) -> VideoResult:
    """Safely creates an updated, validated VideoResult without mutating the original."""
    return video.update_validated(**updates)


class VideoOut(VideoResult):
    imported_from: str | None = None
    model_config = ConfigDict(from_attributes=True, frozen=True)
    id: str
    status: VideoStatus
    search_query: str
    search_job_id: str
    relevance_score: float
    quality_score: float
    final_score: float
    velocity_score: float = 0.0
    virality_score: float = 0.0
    script_analysis: dict | None = None
    created_at: datetime
    updated_at: datetime

    @field_validator("created_at", "updated_at")
    @classmethod
    def timestamps_utc(cls, value: datetime) -> datetime:
        return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value


class VideoPatch(BaseModel):
    status: VideoStatus


class PlatformErrorCode(str, Enum):
    LOGIN_REQUIRED = "LOGIN_REQUIRED"
    CAPTCHA_REQUIRED = "CAPTCHA_REQUIRED"
    NAVIGATION_FAILED = "NAVIGATION_FAILED"
    SELECTOR_CHANGED = "SELECTOR_CHANGED"
    SESSION_EXPIRED = "SESSION_EXPIRED"
    NETWORK_BLOCKED = "NETWORK_BLOCKED"


class JobOut(BaseModel):
    import_source: str | None = None
    queries: list[str] = Field(default_factory=list)
    ai_source: str | None = None
    ai_warning: str | None = None
    query_expansion: dict | None = None
    actual_ai_model: str | None = None
    ai_fallback_occurred: bool = False
    ai_latency_ms: float | None = None
    diagnostics: dict | None = None
    model_config = ConfigDict(from_attributes=True)
    id: str
    original_query: str
    platforms: list[str]
    status: str
    requested_limit: int
    found_count: int
    processed_count: int
    duplicate_count: int
    rejected_count: int = 0
    provider_counts: dict[str, int]
    created_at: datetime
    started_at: datetime | None
    completed_at: datetime | None
    error_message: str | None
    is_mock: bool
    active_platform: str | None = None
    waiting_reason: str | None = None

    @field_validator("created_at", "started_at", "completed_at")
    @classmethod
    def timestamps_utc(cls, value: datetime | None) -> datetime | None:
        return value.replace(tzinfo=timezone.utc) if value and value.tzinfo is None else value


class MetricSnapshotOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    video_id: str
    like_count: int | None = None
    comment_count: int | None = None
    share_count: int | None = None
    favorite_count: int | None = None
    view_count: int | None = None
    captured_at: datetime
    source: str = "search"
    delta_likes: int | None = None
    delta_comments: int | None = None
    delta_shares: int | None = None
    delta_favorites: int | None = None
    delta_views: int | None = None
    elapsed_seconds: float | None = None

    @field_validator("captured_at")
    @classmethod
    def captured_at_utc(cls, value: datetime) -> datetime:
        return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value


class MetricHistoryOut(BaseModel):
    video_id: str
    total_snapshots: int
    snapshots: list[MetricSnapshotOut]


class ViralityBreakdownOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    video_id: str
    virality_score: float
    velocity_score: float
    hourly_velocity: float
    acceleration: float
    age_bucket: str
    spread_ratio: float

