from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=ROOT / ".env", extra="ignore")
    ai_provider: str = ""
    ai_api_key: SecretStr = SecretStr("")
    ai_model: str = "gemini-3.1-flash-lite"
    ai_free_tier_confirmed: bool = False
    ai_timeout: float = Field(20, ge=1, le=30)
    ai_min_interval: float = Field(6, ge=0, le=60)
    # Optional shared-secret for the local API. Empty = auth disabled (default,
    # keeps the localhost UX unchanged). When set, every request except the
    # allowlisted health/events/screenshot paths must carry a matching
    # X-API-Token header (injected server-side by the Next.js proxy).
    api_token: SecretStr = SecretStr("")
    xhs_browser_channel: Literal["chrome", "msedge", "chromium"] = "chrome"
    # Empty = inherit xhs_browser_channel (preserves existing profiles).
    # Set to msedge to keep Douyin on its own browser single-instance.
    douyin_browser_channel: Literal["", "chrome", "msedge", "chromium"] = ""
    app_env: str = "development"
    database_url: str = "sqlite:///./data/app.db"
    use_mock_provider: bool = False
    default_result_limit: int = Field(50, ge=1, le=200)
    max_queries: int = Field(10, ge=1, le=10)
    search_timeout: int = Field(300, ge=10, le=600)
    # Re-running the exact same search within this window reuses the previous
    # completed job's results instantly (0 disables). Matched on query,
    # platforms, filters, AI flag and limit.
    search_cache_ttl_seconds: int = Field(3600, ge=0, le=86400)
    download_dir: Path = ROOT / "data" / "downloads"
    dubbing_dir: Path = ROOT / "data" / "dubbing"
    max_concurrent_downloads: int = Field(2, ge=1, le=10)
    max_concurrent_per_platform: int = Field(1, ge=1, le=5)
    download_timeout_seconds: int = Field(120, ge=10, le=600)
    max_download_size_bytes: int = Field(209_715_200, ge=1048576)  # 200MB default
    xhs_downloader_url: str = "http://127.0.0.1:5556"
    douyin_downloader_url: str = "http://127.0.0.1:5555"
    xhs_cookie: str = ""
    douyin_cookie: str = ""
    download_queue_capacity: int = Field(100, ge=10, le=1000)
    download_worker_count: int = Field(2, ge=1, le=10)
    download_max_retries: int = Field(3, ge=0, le=5)
    download_retry_base_delay: float = Field(2.0, ge=0.5, le=30.0)
    playwright_headless: bool = True
    hybrid_headless: bool = True
    browser_auto_elevate: bool = True
    search_headless_silent: bool = True
    browser_interaction_timeout: int = Field(1800, ge=10, le=7200)
    metric_snapshot_throttle_seconds: int = Field(300, ge=1, le=86400)
    # inspect() runs once per scroll iteration; checkpointing (storage_state +
    # fsync) every time stalls the search. Save at most this often — _close()
    # and explicit user-action paths (drag/auto-solve) always checkpoint.
    session_checkpoint_min_interval: float = Field(30.0, ge=0.0, le=3600.0)


settings = Settings()
