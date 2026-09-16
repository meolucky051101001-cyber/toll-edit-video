from collections.abc import Mapping
from datetime import datetime, timezone
from typing import Any, Literal, Self
from urllib.parse import urlparse

from pydantic import BaseModel, ConfigDict, Field, HttpUrl, field_validator, model_validator

Platform = Literal["douyin", "xiaohongshu"]
VideoStatus = Literal["new", "saved", "skipped", "used", "favorite"]


class SearchRequest(BaseModel):
    mode: Literal["mock", "xiaohongshu", "douyin"] | None = None
    use_ai: bool = False
    selected_queries: list[str] | None = Field(default=None, min_length=1, max_length=10)
    query: str = Field(min_length=1, max_length=300)
    platforms: list[Platform] = Field(default=["douyin", "xiaohongshu"], min_length=1, max_length=2)
    limit: int = Field(default=50, ge=1, le=200)

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
    else:
        trusted = ("xiaohongshu.com", "rednote.com", "xhslink.com", "douyin.com", "iesdouyin.com")

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
    is_mock: bool = False

    @model_validator(mode="before")
    @classmethod
    def infer_mock_and_identity(cls, data: Any) -> Any:
        if isinstance(data, dict):
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
    script_analysis: dict | None = None
    created_at: datetime
    updated_at: datetime

    @field_validator("created_at", "updated_at")
    @classmethod
    def timestamps_utc(cls, value: datetime) -> datetime:
        return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value


class VideoPatch(BaseModel):
    status: VideoStatus


class JobOut(BaseModel):
    import_source: str | None = None
    queries: list[str] = Field(default_factory=list)
    ai_source: str | None = None
    ai_warning: str | None = None
    query_expansion: dict | None = None
    model_config = ConfigDict(from_attributes=True)
    id: str
    original_query: str
    platforms: list[str]
    status: str
    requested_limit: int
    found_count: int
    processed_count: int
    duplicate_count: int
    provider_counts: dict[str, int]
    created_at: datetime
    started_at: datetime | None
    completed_at: datetime | None
    error_message: str | None
    is_mock: bool

    @field_validator("created_at", "started_at", "completed_at")
    @classmethod
    def timestamps_utc(cls, value: datetime | None) -> datetime | None:
        return value.replace(tzinfo=timezone.utc) if value and value.tzinfo is None else value
