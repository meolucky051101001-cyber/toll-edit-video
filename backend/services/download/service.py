import asyncio
import logging
import re
import shutil
import time
from typing import Any
from pathlib import Path
from urllib.parse import urljoin, urlsplit

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from backend.core.config import settings
from backend.models.download_job import DownloadJob
from backend.models.video import Video
from backend.schemas.downloads import DownloadJobOut, ResolvedMedia
from backend.services.download.base import BaseDownloadAdapter, DownloadAdapterError
from backend.services.download.douyin_adapter import DouyinDownloadAdapter
from backend.services.download.network import (
    create_safe_async_client,
    is_safe_media_url_async,
    is_safe_media_url_sync,
)
from backend.services.download.validator import (
    is_valid_mp4,
    parse_iso_bmff_structure,
    probe_media_file_sync,
    validate_video_file_async,
)
from backend.services.download.xhs_adapter import XhsDownloadAdapter

logger = logging.getLogger("download")

# Backwards compatibility aliases
is_safe_media_url = is_safe_media_url_sync
_parse_iso_bmff = parse_iso_bmff_structure


def _probe_video_file(fp: Path) -> bool:
    return probe_media_file_sync(fp)[0]


__all__ = [
    "DownloadService",
    "is_safe_media_url",
    "is_valid_mp4",
    "_parse_iso_bmff",
    "_probe_video_file",
]

class DownloadService:
    def __init__(
        self,
        sessions: sessionmaker[Session],
        xhs_adapter: BaseDownloadAdapter | None = None,
        douyin_adapter: BaseDownloadAdapter | None = None,
        download_dir: Path | str | None = None,
        browser_resolver: Any | None = None,
    ):
        self.sessions = sessions
        self.xhs_adapter = xhs_adapter or XhsDownloadAdapter(settings.xhs_downloader_url)
        self.douyin_adapter = douyin_adapter or DouyinDownloadAdapter(settings.douyin_downloader_url)
        self.browser_resolver = browser_resolver

        self.global_sem = asyncio.Semaphore(settings.max_concurrent_downloads)
        self.platform_sems = {
            "xiaohongshu": asyncio.Semaphore(settings.max_concurrent_per_platform),
            "douyin": asyncio.Semaphore(settings.max_concurrent_per_platform),
        }
        self.cancelled_jobs: set[str] = set()
        self.active_tasks: dict[str, asyncio.Task[None]] = {}

        # Ensure download directory structure exists
        self.download_dir = Path(download_dir or settings.download_dir)
        self.temp_dir = self.download_dir / ".temp"
        self.temp_dir.mkdir(parents=True, exist_ok=True)

    def get_adapter(self, platform: str) -> BaseDownloadAdapter:
        if platform == "xiaohongshu":
            return self.xhs_adapter
        if platform == "douyin":
            return self.douyin_adapter
        raise ValueError(f"Nền tảng '{platform}' chưa hỗ trợ tải video.")

    def recover_interrupted_jobs(self) -> int:
        """Marks any dangling in-progress jobs from previous server runs as interrupted."""
        count = 0
        with self.sessions() as db:
            dangling = db.scalars(
                select(DownloadJob).where(
                    DownloadJob.status.in_(["queued", "resolving", "downloading"])
                )
            ).all()
            for job in dangling:
                job.status = "interrupted"
                job.error_code = "server_restarted"
                job.error_message = "Tiến trình tải bị gián đoạn do server khởi động lại. Vui lòng bấm Thử lại."
                count += 1
            if count > 0:
                db.commit()

        # Clean up dangling .part files
        try:
            if self.temp_dir.exists():
                for part in self.temp_dir.glob("*.part"):
                    try:
                        part.unlink(missing_ok=True)
                    except Exception:
                        pass
        except Exception:
            pass

        return count

    async def shutdown(self) -> None:
        """Gracefully shuts down active download tasks, cancels them, cleans up partial files,
        and marks unfinished jobs as interrupted in the database before disposal."""
        if self.active_tasks:
            logger.info("Shutting down DownloadService with %d active tasks...", len(self.active_tasks))

            for job_id, task in list(self.active_tasks.items()):
                self.cancelled_jobs.add(job_id)
                if not task.done():
                    task.cancel()

            await asyncio.gather(*self.active_tasks.values(), return_exceptions=True)
            self.active_tasks.clear()

        # Clean up any partial files
        try:
            if self.temp_dir.exists():
                for part_file in self.temp_dir.glob("*.part"):
                    try:
                        part_file.unlink(missing_ok=True)
                    except Exception:
                        pass
        except Exception as e:
            logger.warning("Error cleaning partial files on shutdown: %s", e)

        # Update in-progress jobs in DB to 'interrupted'
        with self.sessions() as db:
            stmt = select(DownloadJob).where(
                DownloadJob.status.in_(["queued", "resolving", "downloading"])
            )
            jobs = list(db.scalars(stmt))
            for j in jobs:
                j.status = "interrupted"
                j.error_code = "server_shutdown"
                j.error_message = "Tiến trình tải bị gián đoạn do máy chủ tắt."
            if jobs:
                db.commit()
                logger.info("Marked %d download jobs as interrupted on shutdown.", len(jobs))

    def get_job_by_id(self, download_id: str) -> DownloadJobOut | None:
        with self.sessions() as db:
            job = db.get(DownloadJob, download_id)
            if not job:
                return None
            return self._to_out(job)

    def get_active_job_for_video(self, video_id: str) -> DownloadJobOut | None:
        with self.sessions() as db:
            # 1. Check for running/active job
            job = db.scalar(
                select(DownloadJob)
                .where(
                    DownloadJob.video_id == video_id,
                    DownloadJob.status.in_(["queued", "resolving", "downloading"]),
                )
                .order_by(DownloadJob.created_at.desc())
                .limit(1)
            )
            if job:
                return self._to_out(job)

            # 2. Check for completed job whose file still exists on disk
            completed = db.scalar(
                select(DownloadJob)
                .where(
                    DownloadJob.video_id == video_id,
                    DownloadJob.status == "completed",
                )
                .order_by(DownloadJob.updated_at.desc())
                .limit(1)
            )
            if completed and completed.relative_path:
                full_path = self.download_dir / completed.relative_path
                if full_path.is_file():
                    return self._to_out(completed)

            # 3. Preserve error state on reload: return latest terminal job (failed, interrupted, cancelled)
            latest_terminal = db.scalar(
                select(DownloadJob)
                .where(
                    DownloadJob.video_id == video_id,
                    DownloadJob.status.in_(["failed", "interrupted", "cancelled"]),
                )
                .order_by(DownloadJob.updated_at.desc())
                .limit(1)
            )
            if latest_terminal:
                return self._to_out(latest_terminal)

            return None

    def request_download(self, video_id: str, force: bool = False) -> DownloadJobOut:
        """Requests a video download. Deduplicates against existing running/completed jobs unless force=True."""
        with self.sessions() as db:
            video = db.get(Video, video_id)
            if not video:
                raise ValueError(f"Không tìm thấy video có ID '{video_id}'.")
            if video.is_mock:
                raise ValueError("Video dữ liệu mẫu không hỗ trợ tải về máy.")

            # Deduplication: return existing active job if running
            existing = db.scalar(
                select(DownloadJob)
                .where(
                    DownloadJob.video_id == video_id,
                    DownloadJob.status.in_(["queued", "resolving", "downloading"]),
                )
                .order_by(DownloadJob.created_at.desc())
                .limit(1)
            )
            if existing:
                return self._to_out(existing)

            # If not force, and already completed and file exists on disk, reuse it
            if not force:
                completed = db.scalar(
                    select(DownloadJob)
                    .where(
                        DownloadJob.video_id == video_id,
                        DownloadJob.status == "completed",
                    )
                    .order_by(DownloadJob.updated_at.desc())
                    .limit(1)
                )
                if completed and completed.relative_path:
                    full_path = self.download_dir / completed.relative_path
                    if full_path.is_file():
                        return self._to_out(completed)

            # Create new queued job
            job = DownloadJob(
                video_id=video_id,
                platform=video.platform,
                status="queued",
            )
            db.add(job)
            db.commit()
            job_id = job.id
            out = self._to_out(job)

        # Dispatch background processor
        self.cancelled_jobs.discard(job_id)
        try:
            loop = asyncio.get_running_loop()
            task = loop.create_task(self._process_download_job(job_id))
            self.active_tasks[job_id] = task
            task.add_done_callback(lambda _: self.active_tasks.pop(job_id, None))
        except RuntimeError:
            pass

        return out

    def request_batch_downloads(self, video_ids: list[str], force: bool = False) -> list[DownloadJobOut]:
        """Enqueues multiple video downloads in a batch, deduplicating valid video IDs."""
        seen: set[str] = set()
        unique_ids: list[str] = []
        for vid in video_ids:
            clean_id = vid.strip() if vid else ""
            if clean_id and clean_id not in seen:
                seen.add(clean_id)
                unique_ids.append(clean_id)

        jobs: list[DownloadJobOut] = []
        for vid in unique_ids:
            try:
                job = self.request_download(vid, force=force)
                jobs.append(job)
            except ValueError as e:
                logger.warning("Skipping invalid video in batch download %s: %s", vid, e)
                continue
        return jobs

    def cancel_download(self, download_id: str) -> DownloadJobOut:
        self.cancelled_jobs.add(download_id)
        task = self.active_tasks.get(download_id)
        if task and not task.done():
            task.cancel()

        with self.sessions() as db:
            job = db.get(DownloadJob, download_id)
            if not job:
                raise ValueError("Không tìm thấy lượt tải này.")
            if job.status in ("queued", "resolving", "downloading"):
                job.status = "cancelled"
                job.error_message = "Đã hủy bởi người dùng."
                db.commit()

            # Clean up .part file if exists
            part_path = self.temp_dir / f"{download_id}.part"
            if part_path.exists():
                try:
                    part_path.unlink(missing_ok=True)
                except Exception:
                    pass

            return self._to_out(job)

    async def _process_download_job(self, download_id: str) -> None:
        if download_id in self.cancelled_jobs:
            return

        with self.sessions() as db:
            job = db.get(DownloadJob, download_id)
            if not job:
                return
            video_id = job.video_id
            platform = job.platform
            video = db.get(Video, video_id)
            if not video:
                self._fail_job(download_id, "video_not_found", "Video không còn tồn tại trong hệ thống.")
                return

        platform_sem = self.platform_sems.get(platform)
        sem_ctx = platform_sem if platform_sem is not None else self.global_sem

        try:
            # Acquire platform semaphore BEFORE global semaphore to prevent head-of-line blocking
            async with sem_ctx:
                async with self.global_sem:
                    if download_id in self.cancelled_jobs:
                        return

                    # 1. Resolve media URL via adapter
                    self._update_job_status(download_id, "resolving")
                    adapter = self.get_adapter(platform)

                    with self.sessions() as db:
                        v = db.get(Video, video_id)
                        assert v is not None
                        target_video = v

                    try:
                        resolved = await adapter.resolve(target_video)
                    except DownloadAdapterError as dae:
                        # Attempt browser in-page resolution fallback if primary adapter had service/upstream/media error
                        fallback_success = False
                        if self.browser_resolver and dae.code in (
                            "service_unavailable",
                            "upstream_error",
                            "upstream_network_error",
                            "upstream_invalid_json",
                            "invalid_response",
                            "no_media_url",
                        ):
                            try:
                                resolved = await self.browser_resolver.resolve(target_video)
                                fallback_success = True
                                logger.info(
                                    "Primary adapter %s failed with %s; successfully resolved via BrowserInPageResolver.",
                                    adapter.name,
                                    dae.code,
                                )
                            except Exception:
                                pass

                        if not fallback_success:
                            self._fail_job(download_id, dae.code, dae.message)
                            return
                    except Exception:
                        fallback_success = False
                        if self.browser_resolver:
                            try:
                                resolved = await self.browser_resolver.resolve(target_video)
                                fallback_success = True
                                logger.info(
                                    "Primary adapter %s failed with unexpected exception; resolved via BrowserInPageResolver.",
                                    adapter.name,
                                )
                            except Exception:
                                pass

                        if not fallback_success:
                            self._fail_job(download_id, "resolve_error", "Không thể lấy liên kết tải từ nền tảng.")
                            return

                    if download_id in self.cancelled_jobs:
                        return

                    # 2. SSRF check on initial media URL (async DNS resolution with caching)
                    if not await is_safe_media_url_async(resolved.media_url):
                        self._fail_job(
                            download_id,
                            "insecure_media_url",
                            "Địa chỉ media không hợp lệ hoặc trỏ tới mạng nội bộ bị cấm.",
                        )
                        return

                    # 3. Stream download to .part file with total job deadline
                    self._update_job_status(
                        download_id,
                        "downloading",
                        adapter_version=resolved.adapter_version,
                    )
                    try:
                        await asyncio.wait_for(
                            self._download_stream(download_id, resolved, target_video),
                            timeout=float(settings.download_timeout_seconds),
                        )
                    except asyncio.TimeoutError:
                        part_path = self.temp_dir / f"{download_id}.part"
                        part_path.unlink(missing_ok=True)
                        self._fail_job(
                            download_id,
                            "download_timeout",
                            "Quá thời gian tối đa cho phép để tải và xác thực video.",
                        )
                        return

        except asyncio.CancelledError:
            self._handle_cancel(download_id)
        except Exception:
            logger.exception("Unexpected error processing download %s", download_id)
            self._fail_job(download_id, "unexpected_error", "Lỗi không xác định khi xử lý tải video.")

    async def _download_stream(
        self,
        download_id: str,
        resolved: ResolvedMedia,
        video: Video,
    ) -> None:
        part_path = self.temp_dir / f"{download_id}.part"
        bytes_written = 0
        total_size: int | None = None

        resume_headers: dict[str, str] = dict(resolved.headers)

        try:
            current_url = resolved.media_url
            max_redirects = 5
            redirect_count = 0
            current_headers = dict(resume_headers)
            original_host = urlsplit(current_url).netloc

            async with create_safe_async_client(
                timeout=httpx.Timeout(settings.download_timeout_seconds, connect=15.0),
            ) as client:
                response: httpx.Response | None = None
                try:
                    while True:
                        if not await is_safe_media_url_async(current_url):
                            self._fail_job(
                                download_id,
                                "insecure_redirect",
                                "Liên kết chuyển hướng trỏ tới địa chỉ mạng nội bộ hoặc không an toàn.",
                            )
                            return

                        # Strip credentials across hosts while maintaining platform Referer to avoid CDN 403 Forbidden
                        new_host = urlsplit(current_url).netloc
                        if new_host != original_host:
                            current_headers.pop("Authorization", None)
                            current_headers.pop("Cookie", None)
                            new_host_lower = new_host.lower()
                            if any(d in new_host_lower for d in ("douyinvod.com", "douyin", "snssdk.com", "bytecdn")):
                                current_headers["Referer"] = "https://www.douyin.com/"
                            elif any(d in new_host_lower for d in ("xhscdn.com", "xiaohongshu", "rednote")):
                                current_headers["Referer"] = "https://www.xiaohongshu.com/"
                            elif any(d in new_host_lower for d in ("tiktokcdn.com", "tiktok.com", "ibytedtos.com")):
                                current_headers["Referer"] = "https://www.tiktok.com/"
                            elif not current_headers.get("Referer") and resolved.headers.get("Referer"):
                                current_headers["Referer"] = resolved.headers["Referer"]

                        if not current_headers.get("User-Agent"):
                            current_headers["User-Agent"] = (
                                resolved.headers.get("User-Agent")
                                or "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
                            )

                        req = client.build_request("GET", current_url, headers=current_headers)
                        resp = await client.send(req, stream=True)

                        if resp.status_code in (301, 302, 303, 307, 308):
                            await resp.aclose()
                            redirect_count += 1
                            if redirect_count > max_redirects:
                                self._fail_job(
                                    download_id,
                                    "too_many_redirects",
                                    "Quá nhiều lần chuyển hướng khi tải media.",
                                )
                                return

                            loc = resp.headers.get("location")
                            if not loc:
                                self._fail_job(
                                    download_id,
                                    "invalid_redirect",
                                    "Phản hồi chuyển hướng thiếu tiêu đề Location.",
                                )
                                return

                            current_url = urljoin(current_url, loc)
                            continue

                        response = resp
                        break

                    # Check HTTP status code (strict: 200 for full download, 206 only when Range requested)
                    has_range = "Range" in current_headers
                    if not has_range:
                        if response.status_code != 200:
                            self._fail_job(
                                download_id,
                                "media_http_error",
                                f"Máy chủ media trả mã lỗi HTTP {response.status_code} (yêu cầu 200).",
                            )
                            return
                    else:
                        if response.status_code != 206:
                            self._fail_job(
                                download_id,
                                "media_http_error",
                                f"Máy chủ media trả mã HTTP {response.status_code} thay vì 206 Partial Content.",
                            )
                            return
                        content_range = response.headers.get("content-range")
                        if not content_range or not content_range.lower().startswith("bytes "):
                            self._fail_job(
                                download_id,
                                "invalid_content_range",
                                "Phản hồi 206 thiếu tiêu đề Content-Range hợp lệ.",
                            )
                            return

                    content_type = response.headers.get("content-type", "").lower()
                    if "text/html" in content_type or "application/json" in content_type:
                        self._fail_job(
                            download_id,
                            "invalid_content_type",
                            "Máy chủ media trả trang web hoặc dữ liệu lỗi thay vì video.",
                        )
                        return

                    c_len = response.headers.get("content-length")
                    if c_len and c_len.isdigit():
                        total_size = int(c_len)
                        if total_size > settings.max_download_size_bytes:
                            self._fail_job(
                                download_id,
                                "file_too_large",
                                f"Dung lượng video ({total_size // (1024*1024)}MB) vượt quá giới hạn cho phép ({settings.max_download_size_bytes // (1024*1024)}MB).",
                            )
                            return

                    # Check available disk space before streaming
                    try:
                        stat_disk = shutil.disk_usage(self.download_dir)
                        required_space = (total_size or 50 * 1024 * 1024) + 100 * 1024 * 1024
                        if stat_disk.free < required_space:
                            self._fail_job(
                                download_id,
                                "insufficient_disk_space",
                                f"Dung lượng ổ đĩa không đủ để tải video (còn trống {stat_disk.free // (1024*1024)}MB).",
                            )
                            return
                    except Exception as e:
                        logger.warning("Could not check disk usage: %s", e)

                    # Update total size in DB
                    with self.sessions() as db:
                        j = db.get(DownloadJob, download_id)
                        if j:
                            j.total_bytes = total_size
                            db.commit()

                    # Write stream
                    last_db_update = time.monotonic()
                    with open(part_path, "wb") as f:
                        async for chunk in response.aiter_bytes(chunk_size=65536):
                            if download_id in self.cancelled_jobs:
                                f.close()
                                part_path.unlink(missing_ok=True)
                                return

                            f.write(chunk)
                            bytes_written += len(chunk)

                            if bytes_written > settings.max_download_size_bytes:
                                f.close()
                                part_path.unlink(missing_ok=True)
                                self._fail_job(
                                    download_id,
                                    "file_too_large",
                                    "Dung lượng video tải về vượt quá giới hạn tối đa cho phép.",
                                )
                                return

                            # Periodic progress update (every 0.5s)
                            now = time.monotonic()
                            if now - last_db_update >= 0.5:
                                last_db_update = now
                                self._update_job_progress(download_id, bytes_written, total_size)
                finally:
                    if response is not None:
                        await response.aclose()

        except httpx.TimeoutException:
            part_path.unlink(missing_ok=True)
            self._fail_job(download_id, "download_timeout", "Quá thời gian tải video từ máy chủ media.")
            return
        except Exception:
            part_path.unlink(missing_ok=True)
            self._fail_job(download_id, "stream_error", "Lỗi gián đoạn khi truyền dữ liệu từ máy chủ media.")
            return

        if download_id in self.cancelled_jobs:
            part_path.unlink(missing_ok=True)
            return

        # 4. Verify downloaded media container (strict ISO-BMFF + async ffprobe)
        if not part_path.is_file() or part_path.stat().st_size == 0:
            part_path.unlink(missing_ok=True)
            self._fail_job(download_id, "empty_file", "Tập tin tải về có dung lượng 0 bytes.")
            return

        is_valid, verify_err = await validate_video_file_async(part_path)
        if not is_valid:
            part_path.unlink(missing_ok=True)
            self._fail_job(
                download_id,
                "invalid_video_format",
                f"Tập tin tải về không phải định dạng video hợp lệ: {verify_err or 'Lỗi định dạng MP4'}",
            )
            return

        # 5. Atomic Rename & Finalize
        file_size = part_path.stat().st_size
        sanitized_id = re.sub(r"[^a-zA-Z0-9_-]", "", video.platform_video_id or video.id[:12])
        final_filename = f"{video.platform}_{sanitized_id}_{download_id[:8]}.mp4"
        final_path = self.download_dir / final_filename

        try:
            shutil.move(str(part_path), str(final_path))
        except Exception:
            self._fail_job(download_id, "storage_error", "Lỗi lưu trữ tập tin hoàn tất.")
            return

        with self.sessions() as db:
            j = db.get(DownloadJob, download_id)
            if j:
                j.status = "completed"
                j.relative_path = final_filename
                j.file_size = file_size
                j.bytes_downloaded = file_size
                j.total_bytes = file_size
                j.error_code = None
                j.error_message = None
                db.commit()

    def _update_job_status(
        self,
        download_id: str,
        status: str,
        adapter_version: str | None = None,
    ) -> None:
        with self.sessions() as db:
            job = db.get(DownloadJob, download_id)
            if job and job.status not in ("cancelled", "failed", "completed"):
                job.status = status
                if adapter_version:
                    job.adapter_version = adapter_version
                db.commit()

    def _update_job_progress(
        self,
        download_id: str,
        bytes_downloaded: int,
        total_bytes: int | None,
    ) -> None:
        with self.sessions() as db:
            job = db.get(DownloadJob, download_id)
            if job and job.status == "downloading":
                job.bytes_downloaded = bytes_downloaded
                if total_bytes:
                    job.total_bytes = total_bytes
                db.commit()

    def _fail_job(self, download_id: str, code: str, message: str) -> None:
        with self.sessions() as db:
            job = db.get(DownloadJob, download_id)
            if job and job.status not in ("cancelled", "completed"):
                job.status = "failed"
                job.error_code = code
                job.error_message = message
                db.commit()

    def _handle_cancel(self, download_id: str) -> None:
        with self.sessions() as db:
            job = db.get(DownloadJob, download_id)
            if job and job.status not in ("completed", "failed"):
                job.status = "cancelled"
                job.error_message = "Đã hủy bởi người dùng."
                db.commit()

        part_path = self.temp_dir / f"{download_id}.part"
        if part_path.exists():
            try:
                part_path.unlink(missing_ok=True)
            except Exception:
                pass

    def _to_out(self, job: DownloadJob) -> DownloadJobOut:
        percent: float | None = None
        if job.total_bytes and job.total_bytes > 0:
            percent = round((job.bytes_downloaded / job.total_bytes) * 100, 1)
        elif job.status == "completed":
            percent = 100.0

        return DownloadJobOut(
            id=job.id,
            video_id=job.video_id,
            platform=job.platform,  # type: ignore[arg-type]
            status=job.status,  # type: ignore[arg-type]
            bytes_downloaded=job.bytes_downloaded,
            total_bytes=job.total_bytes,
            progress_percent=percent,
            file_size=job.file_size,
            error_code=job.error_code,
            error_message=job.error_message,
            adapter_version=job.adapter_version,
            created_at=job.created_at,
            updated_at=job.updated_at,
        )
