import asyncio
import inspect
import logging
import re
import time
from collections.abc import Callable
from datetime import timedelta
from typing import cast

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from backend.core.config import settings
from backend.core.events import EventBus
from backend.core.events import event_bus as default_event_bus
from backend.models import JobVideo, SearchJob, SearchPlan, SearchQuery, Video
from backend.models.video import utcnow
from backend.providers.base import ProviderError, SearchProvider, UserActionRequired
from backend.providers.registry import get_provider
from backend.schemas.contracts import Platform, SearchFilters, VideoResult
from backend.schemas.expansion import ExpansionOutcome
from backend.services.ai_service import AIService
from backend.services.dedup_service import canonical_url, find_existing, fingerprint
from backend.services.identity_policy import (
    is_xhs_short_link,
    merge_share_url,
    resolve_xhs_short_link_async,
    validate_result_identity,
)
from backend.services.metric_service import MetricService
from backend.services.ranking_service import rank_detailed
from backend.services.translator import is_cjk, plan_query_async

TERMINAL = {"completed", "failed", "cancelled", "interrupted"}
logger = logging.getLogger("research")


class SearchService:
    def __init__(
        self,
        sessions: sessionmaker[Session],
        factory: Callable[[Platform, bool], SearchProvider] = get_provider,
        ai: AIService | None = None,
        event_bus: EventBus | None = None,
        metric_service: MetricService | None = None,
    ):
        self.sessions = sessions
        self.factory = factory
        self.ai = ai
        self.event_bus = event_bus if event_bus is not None else default_event_bus
        self.metric_service = (
            metric_service
            if metric_service is not None
            else MetricService(
                sessions=sessions,
                event_bus=self.event_bus,
                throttle_seconds=settings.metric_snapshot_throttle_seconds,
            )
        )
        self.tasks: dict[str, asyncio.Task[None]] = {}
        self.lock = asyncio.Lock()
        self.platform_locks: dict[str, asyncio.Lock] = {}
        self.waiters: dict[str, asyncio.Event] = {}
        self.deadlines: dict[str, asyncio.Timeout] = {}
        self.waiting_contexts: dict[str, tuple[SearchProvider, UserActionRequired, str]] = {}

    def get_platform_lock(self, platform: str) -> asyncio.Lock:
        if platform not in self.platform_locks:
            self.platform_locks[platform] = asyncio.Lock()
        return self.platform_locks[platform]

    def submit(self, job_id: str) -> None:
        task = asyncio.create_task(self.run(job_id))
        self.tasks[job_id] = task
        task.add_done_callback(lambda _: self.tasks.pop(job_id, None))

    def update(self, job_id: str, **values: object) -> None:
        with self.sessions() as db:
            job = db.get(SearchJob, job_id)
            if job:
                diag = dict(job.diagnostics or {})
                for key, value in values.items():
                    if hasattr(job, key):
                        setattr(job, key, value)
                    if key in ("active_platform", "waiting_reason"):
                        diag[key] = value
                job.diagnostics = diag
                db.commit()
                if self.event_bus:
                    self.event_bus.publish_nowait("search:update", {
                        "job_id": job.id,
                        "status": job.status,
                        "processed_count": job.processed_count,
                        "requested_limit": job.requested_limit,
                        "found_count": job.found_count,
                        "error_message": job.error_message,
                        "active_platform": getattr(job, "active_platform", None) or diag.get("active_platform"),
                        "waiting_reason": getattr(job, "waiting_reason", None) or diag.get("waiting_reason"),
                    })

    async def cancel(self, job_id: str) -> None:
        self.waiting_contexts.pop(job_id, None)
        event = self.waiters.get(job_id)
        if event:
            event.set()
        task = self.tasks.get(job_id)
        if task:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        with self.sessions() as db:
            job = db.get(SearchJob, job_id)
            if job and job.status not in TERMINAL:
                job.status = "cancelled"
                job.completed_at = utcnow()
                if job.processed_count > 0:
                    job.error_message = (
                        f"Đã dừng theo yêu cầu. Đã lưu an toàn {job.processed_count} video trong phiên."
                    )
                db.commit()
                if self.event_bus:
                    self.event_bus.publish_nowait("search:update", {
                        "job_id": job.id,
                        "status": "cancelled",
                        "processed_count": job.processed_count,
                        "requested_limit": job.requested_limit,
                        "found_count": job.found_count,
                        "error_message": job.error_message,
                    })

    async def shutdown(self) -> None:
        for job_id in list(self.tasks):
            await self.cancel(job_id)

    def recover_interrupted(self) -> None:
        with self.sessions() as db:
            for job in db.scalars(select(SearchJob).where(SearchJob.status.not_in(TERMINAL))):
                if job.processed_count >= job.requested_limit:
                    job.status = "completed"
                    job.error_message = None
                elif job.processed_count > 0:
                    job.status = "interrupted"
                    job.error_message = (
                        f"Lượt tìm bị gián đoạn khi ứng dụng khởi động lại. Đã lưu an toàn {job.processed_count}/{job.requested_limit} video."
                    )
                else:
                    job.status = "failed"
                    job.error_message = "Ứng dụng đã dừng khi đang tìm. Bạn có thể chạy lại từ Lịch sử."
                job.completed_at = utcnow()
            db.commit()

    async def run(self, job_id: str) -> None:
        start_time = time.perf_counter()
        try:
            await self.pipeline(job_id)
            duration_ms = (time.perf_counter() - start_time) * 1000.0
            from backend.services.observability import telemetry_service
            telemetry_service.record_latency("search_crawl_ms", duration_ms)
            telemetry_service.record_counter("searches_completed", 1)
        except asyncio.CancelledError:
            self.update(job_id, status="cancelled", completed_at=utcnow())
            raise
        except TimeoutError:
            self.update(
                job_id,
                status="failed",
                completed_at=utcnow(),
                error_message="Tìm kiếm quá thời gian cho phép. Hãy giảm số kết quả và thử lại.",
            )
        except Exception as error:
            message = (
                str(error)
                if isinstance(error, ProviderError)
                else "Không thể hoàn tất tìm kiếm. Xem logs/app.log và thử lại."
            )
            logger.error("job=%s provider=- query=- error_type=%s", job_id, type(error).__name__)
            self.update(job_id, status="failed", completed_at=utcnow(), error_message=message)

    async def pipeline(self, job_id: str) -> None:
        self.update(job_id, status="searching", started_at=utcnow())
        with self.sessions() as db:
            job = db.get(SearchJob, job_id)
            if not job:
                return
            query, platforms, limit = job.original_query, job.platforms, job.requested_limit
            use_mock = job.is_mock
            plan = db.get(SearchPlan, job_id)
            plan_filters = plan.filters if plan and plan.filters else {}
            job_filters = SearchFilters(**plan_filters) if plan_filters else SearchFilters()
        queries = await self.prepare_queries(job_id, query)
        if not use_mock:
            queries = queries[:3]
        for platform_index, platform in enumerate(platforms):
            provider_limit = limit // len(platforms) + (platform_index < limit % len(platforms))
            if provider_limit == 0:
                continue
            self.update(job_id, active_platform=platform)
            lock_key = "mock" if use_mock else platform
            async with self.get_platform_lock(lock_key):
                async with asyncio.timeout(settings.search_timeout) as deadline:
                    self.deadlines[job_id] = deadline
                    provider = self.factory(cast(Platform, platform), use_mock)
                    try:
                        for index, term in enumerate(queries):
                            with self.sessions() as db:
                                current = db.get(SearchJob, job_id)
                                assert current is not None
                                remaining = provider_limit - current.provider_counts.get(platform, 0)
                                if remaining <= 0 or current.processed_count >= current.requested_limit:
                                    break
                                db.add(SearchQuery(job_id=job_id, platform=platform, query=term))
                                db.commit()
                            if use_mock:
                                query_limit = max(
                                    1, (remaining + len(queries) - index - 1) // (len(queries) - index)
                                )
                            else:
                                query_limit = remaining
                            self.update(job_id, status="searching", active_platform=platform)

                            streamed = False

                            async def on_batch(batch: list[VideoResult]) -> None:
                                nonlocal streamed
                                streamed = True
                                await self.persist_results(
                                    job_id,
                                    platform,
                                    term,
                                    batch,
                                    provider_limit,
                                    alternative_queries=queries,
                                    original_query=query,
                                )

                            while True:
                                try:
                                    sig = inspect.signature(provider.search)
                                    if "on_batch" in sig.parameters:
                                        results = await provider.search(
                                            term,
                                            query_limit,
                                            job_filters,
                                            on_batch=on_batch,
                                        )
                                    else:
                                        results = await provider.search(
                                            term,
                                            query_limit,
                                            job_filters,
                                        )
                                    break
                                except UserActionRequired as gate:
                                    waiting_reason = "login_required" if gate.state == "waiting_for_login" else "verification_required"
                                    self.update(
                                        job_id,
                                        active_platform=platform,
                                        waiting_reason=waiting_reason,
                                    )
                                    if gate.partial and not streamed:
                                        await self.persist_results(
                                            job_id,
                                            platform,
                                            term,
                                            gate.partial,
                                            provider_limit,
                                            alternative_queries=queries,
                                            original_query=query,
                                        )
                                    await self.wait_for_user(job_id, gate, provider=provider, platform=platform)
                            logger.info(
                                "job=%s provider=%s query=%r results=%d",
                                job_id,
                                platform,
                                term,
                                len(results),
                            )
                            if not streamed and results:
                                self.update(job_id, status="ranking")
                                await self.persist_results(
                                    job_id,
                                    platform,
                                    term,
                                    results,
                                    provider_limit,
                                    alternative_queries=queries,
                                    original_query=query,
                                )
                            with self.sessions() as db:
                                current = db.get(SearchJob, job_id)
                                if current and (
                                    current.provider_counts.get(platform, 0) >= provider_limit
                                    or current.processed_count >= current.requested_limit
                                ):
                                    break
                    finally:
                        self.deadlines.pop(job_id, None)
                        await provider.close()
        with self.sessions() as db:
            job = db.get(SearchJob, job_id)
            if job and job.status not in TERMINAL:
                job.status = "completed"
                job.completed_at = utcnow()
                if job.processed_count == 0 and job.found_count == 0:
                    job.error_message = "Không tìm thấy video nào phù hợp với từ khóa và bộ lọc."
                db.commit()

    async def prepare_queries(self, job_id: str, original: str) -> list[str]:
        with self.sessions() as db:
            plan = db.get(SearchPlan, job_id)
            if not plan:
                return [original]
            if plan.queries:
                return plan.queries[: settings.max_queries]
            use_ai = plan.use_ai
            owner = db.get(SearchJob, job_id)
            is_real_cjk_platform = bool(
                owner
                and not owner.is_mock
                and any(p in ("xiaohongshu", "douyin") for p in owner.platforms)
            )

        cleaned = re.sub(r"\bubox\b", "unbox", original, flags=re.IGNORECASE)
        cleaned = re.sub(r"\buboxing\b", "unboxing", cleaned, flags=re.IGNORECASE)

        outcome = None
        if use_ai and self.ai:
            self.update(job_id, status="expanding_query")
            outcome = await self.ai.expand(cleaned)
            if is_real_cjk_platform and (
                outcome.source == "fallback" or not is_cjk(" ".join(outcome.queries))
            ):
                q_plan = await plan_query_async(cleaned)
                if q_plan.tiered_queries and is_cjk(" ".join(q_plan.tiered_queries)):
                    outcome.queries = q_plan.tiered_queries
                    outcome.plan = q_plan
                    outcome.source = "dictionary"
        elif is_real_cjk_platform and not is_cjk(original):
            q_plan = await plan_query_async(cleaned)
            if q_plan.tiered_queries:
                outcome = ExpansionOutcome(
                    original_query=original,
                    source="dictionary",
                    plan=q_plan,
                    queries=q_plan.tiered_queries,
                )

        with self.sessions() as db:
            plan = db.get(SearchPlan, job_id)
            assert plan is not None
            owner = db.get(SearchJob, job_id)
            if owner and outcome:
                owner.actual_ai_model = outcome.actual_model
                owner.ai_fallback_occurred = outcome.fallback_occurred
                owner.ai_latency_ms = outcome.latency_ms
            plan.queries = (outcome.queries if outcome else [original])[
                : settings.max_queries if owner and owner.is_mock else 3
            ]
            plan.source = outcome.source if outcome else "original"
            plan.warning = outcome.warning if outcome else None
            plan.expansion = (
                outcome.expansion.model_dump() if outcome and outcome.expansion else None
            )
            db.commit()
            return plan.queries[: settings.max_queries]

    async def persist_results(
        self,
        job_id: str,
        platform: str,
        query: str,
        results: list[VideoResult],
        provider_limit: int,
        alternative_queries: list[str] | None = None,
        original_query: str | None = None,
    ) -> None:
        ranking_target = original_query or query
        for start in range(0, len(results), 8):
            batch = results[start : start + 8]
            validated_batch: list[VideoResult] = []
            for item in batch:
                is_valid, resolved_id, err = validate_result_identity(
                    item.platform,
                    item.platform_video_id,
                    str(item.url),
                    item.share_url,
                    is_mock=item.is_mock,
                )
                if not is_valid:
                    logger.warning("Quarantine candidate with contradictory identity: %s", err)
                    continue
                if item.platform_video_id != resolved_id:
                    item = item.update_validated(platform_video_id=resolved_id)
                if item.share_url and is_xhs_short_link(item.share_url):
                    resolved_short = await resolve_xhs_short_link_async(
                        item.share_url, expected_id=item.platform_video_id
                    )
                    if resolved_short:
                        item = item.update_validated(share_url=resolved_short)
                validated_batch.append(item)

            with self.sessions() as db:
                job = db.get(SearchJob, job_id)
                assert job is not None
                plan = db.get(SearchPlan, job_id)
                plan_filters = plan.filters if plan and plan.filters else {}
                job_filters = SearchFilters(**plan_filters) if plan_filters else SearchFilters()
                for result in validated_batch:
                    job.found_count += 1
                    detail = rank_detailed(
                        result,
                        ranking_target,
                        alternative_queries=alternative_queries,
                        exact_hashtag=bool(job_filters.exact_hashtag or ranking_target.strip().startswith("#")),
                        target_hashtag=job_filters.target_hashtag,
                    )
                    scores = (detail.relevance, detail.quality, detail.final_score)

                    is_rejected = False
                    reject_reason = None

                    # Defense-in-depth: content_type filter
                    if job_filters.content_type != "all":
                        if job_filters.content_type == "video" and result.media_type != "video":
                            is_rejected = True
                            reject_reason = "Định dạng không khớp: chỉ lấy video"
                        elif job_filters.content_type == "image_text" and result.media_type != "image_text":
                            is_rejected = True
                            reject_reason = "Định dạng không khớp: chỉ lấy bài viết / hình ảnh"

                    # Defense-in-depth: publish_time filter
                    if not is_rejected and job_filters.publish_time != "all" and result.published_at:
                        now = utcnow()
                        delta_map = {
                            "day": timedelta(days=1),
                            "week": timedelta(days=7),
                            "six_months": timedelta(days=182),
                        }
                        max_age = delta_map.get(job_filters.publish_time)
                        if max_age and (now - result.published_at) > max_age:
                            is_rejected = True
                            reject_reason = f"Thời gian đăng không thỏa mãn bộ lọc ({job_filters.publish_time})"

                    # Strict hashtag match guard
                    if not is_rejected and (job_filters.exact_hashtag or ranking_target.strip().startswith("#")):
                        if detail.missing_mandatory_intents and any(m.startswith("#") for m in detail.missing_mandatory_intents):
                            is_rejected = True
                            reject_reason = f"Không khớp thẻ hashtag yêu cầu ({job_filters.target_hashtag or ranking_target.strip()})"

                    if not is_rejected and job.min_relevance is not None:
                        if detail.missing_mandatory_intents:
                            is_rejected = True
                            reject_reason = f"Thiếu ý định bắt buộc: {', '.join(detail.missing_mandatory_intents)}"
                        elif detail.relevance < job.min_relevance:
                            is_rejected = True
                            reject_reason = f"Độ liên quan thấp ({detail.relevance:.1f} < {job.min_relevance:.1f})"
                        elif detail.matched_negative_tokens:
                            is_rejected = True
                            reject_reason = f"Trùng từ khóa tiêu cực: {', '.join(detail.matched_negative_tokens)}"

                    if is_rejected:
                        job.rejected_count += 1
                        diag = dict(job.diagnostics or {})
                        rejected_list = list(diag.get("rejected_candidates", []))
                        if len(rejected_list) < 50:
                            rejected_list.append({
                                "video_id": result.platform_video_id,
                                "platform": platform,
                                "title": result.title or "(Không tiêu đề)",
                                "url": str(result.url),
                                "relevance": round(detail.relevance, 1),
                                "reason": reject_reason,
                            })
                        diag["rejected_candidates"] = rejected_list
                        job.diagnostics = diag
                        continue

                    existing = find_existing(db, result)
                    if existing is None and (
                        job.processed_count >= job.requested_limit
                        or job.provider_counts.get(platform, 0) >= provider_limit
                    ):
                        continue
                    if existing is None:
                        values = result.model_dump(mode="python")
                        values["url"] = str(result.url)
                        canon_u = canonical_url(str(result.url))
                        values["share_url"] = merge_share_url(
                            result.platform,
                            result.platform_video_id,
                            canon_u,
                            None,
                            str(result.share_url) if result.share_url else None,
                        )
                        values.pop("velocity_score", None)
                        values.pop("virality_score", None)
                        existing = Video(
                            **values,
                            canonical_url=canon_u,
                            fingerprint=fingerprint(result),
                            search_query=query,
                            search_job_id=job_id,
                            relevance_score=scores[0],
                            quality_score=scores[1],
                            final_score=scores[2],
                            velocity_score=detail.velocity_score,
                            virality_score=detail.virality_score,
                        )
                        db.add(existing)
                        db.flush()
                        self.metric_service.record_snapshot(
                            video_id=existing.id,
                            like_count=existing.like_count,
                            comment_count=existing.comment_count,
                            share_count=existing.share_count,
                            favorite_count=existing.favorite_count,
                            view_count=existing.view_count,
                            source="search",
                            db=db,
                        )
                    else:
                        job.duplicate_count += 1
                        existing.share_url = merge_share_url(
                            existing.platform,
                            existing.platform_video_id,
                            existing.canonical_url,
                            existing.share_url,
                            str(result.share_url) if result.share_url else None,
                        )
                        if not existing.title and result.title:
                            existing.title = result.title
                        if not existing.caption and result.caption:
                            existing.caption = result.caption
                        if not existing.thumbnail_url and result.thumbnail_url:
                            existing.thumbnail_url = str(result.thumbnail_url)
                        if not existing.duration_seconds and result.duration_seconds:
                            existing.duration_seconds = result.duration_seconds
                        if not existing.author_name and result.author_name:
                            existing.author_name = result.author_name
                        if not existing.author_id and result.author_id:
                            existing.author_id = result.author_id
                        if not existing.author_url and result.author_url:
                            existing.author_url = str(result.author_url)
                        if not existing.published_at and result.published_at:
                            existing.published_at = result.published_at
                        if not existing.hashtags and result.hashtags:
                            existing.hashtags = result.hashtags
                        if getattr(result, "media_type", None) and existing.media_type != result.media_type:
                            existing.media_type = result.media_type
                        if getattr(result, "images", None) and not existing.images:
                            existing.images = result.images
                        existing.velocity_score = detail.velocity_score
                        existing.virality_score = detail.virality_score
                        for field in (
                            "like_count",
                            "comment_count",
                            "share_count",
                            "favorite_count",
                            "view_count",
                        ):
                            value = getattr(result, field)
                            if value is not None:
                                setattr(existing, field, value)
                        self.metric_service.record_snapshot(
                            video_id=existing.id,
                            like_count=existing.like_count,
                            comment_count=existing.comment_count,
                            share_count=existing.share_count,
                            favorite_count=existing.favorite_count,
                            view_count=existing.view_count,
                            source="search",
                            db=db,
                        )
                    if (
                        job.processed_count < job.requested_limit
                        and job.provider_counts.get(platform, 0) < provider_limit
                        and not db.get(JobVideo, (job_id, existing.id))
                    ):
                        db.add(
                            JobVideo(
                                job_id=job_id,
                                video_id=existing.id,
                                relevance_score=scores[0],
                                quality_score=scores[1],
                                final_score=scores[2],
                                velocity_score=detail.velocity_score,
                                virality_score=detail.virality_score,
                            )
                        )
                        job.processed_count += 1
                        counts = dict(job.provider_counts)
                        counts[platform] = counts.get(platform, 0) + 1
                        job.provider_counts = counts
                    db.flush()
                db.commit()
            await asyncio.sleep(0.12)

    def resume(self, job_id: str) -> bool:
        event = self.waiters.get(job_id)
        if not event:
            return False
        self.update(job_id, status="resuming", waiting_reason=None)
        event.set()
        return True

    def resume_all_waiting(self) -> int:
        count = 0
        for job_id, event in list(self.waiters.items()):
            self.update(job_id, status="resuming", waiting_reason=None)
            event.set()
            count += 1
        return count

    async def verify_and_resume(self, job_id: str) -> tuple[bool, str]:
        event = self.waiters.get(job_id)
        if not event:
            return False, "Lượt tìm này không ở trạng thái chờ tiếp tục hoặc đã bị hủy/dừng."

        context = self.waiting_contexts.get(job_id)
        if not context:
            self.update(job_id, status="resuming", waiting_reason=None)
            event.set()
            return True, "Sẵn sàng tiếp tục."

        provider, gate, platform = context
        browser = getattr(provider, "browser", None)
        if not browser:
            self.update(job_id, status="resuming", waiting_reason=None)
            event.set()
            return True, "Sẵn sàng tiếp tục."

        page = None
        if hasattr(browser, "get_active_page"):
            try:
                page = browser.get_active_page()
            except Exception:
                pass
        if not page and hasattr(browser, "page"):
            page = browser.page

        if not page or getattr(page, "is_closed", lambda: True)():
            return False, f"Trình duyệt {platform.capitalize()} đã bị đóng. Vui lòng bấm 'Mở cửa sổ' để mở lại trình duyệt."

        try:
            current_state = await browser.inspect(page)
        except Exception as e:
            return False, f"Lỗi khi kiểm tra trình duyệt {platform.capitalize()}: {e}"

        is_login_wait = gate.state == "waiting_for_login" or "login" in str(gate).lower()
        if is_login_wait:
            is_auth = (
                current_state == "authenticated"
                or bool(getattr(browser, "diagnostics", {}).get("is_authenticated"))
            )
            if not is_auth:
                return (
                    False,
                    f"Trình duyệt {platform.capitalize()} chưa hoàn tất đăng nhập (trạng thái: {current_state}). "
                    f"Vui lòng quét mã QR hoặc đăng nhập tài khoản trên cửa sổ trình duyệt trước khi tiếp tục.",
                )
        else:
            # Waiting for challenge / CAPTCHA
            if current_state in ("verification_required", "restricted", "network_error"):
                return (
                    False,
                    f"Thử thách bảo mật (CAPTCHA) trên {platform.capitalize()} chưa được giải xong. "
                    f"Vui lòng kéo thanh trượt xác minh trên màn hình trình duyệt.",
                )
            if current_state == "login_required":
                return (
                    False,
                    f"Nền tảng {platform.capitalize()} chuyển sang yêu cầu đăng nhập. Vui lòng đăng nhập tài khoản.",
                )

        # Verification passed
        self.update(job_id, status="resuming", waiting_reason=None)
        event.set()
        return True, "Xác minh thành công, đang tiếp tục tìm kiếm."

    async def wait_for_user(
        self,
        job_id: str,
        gate: UserActionRequired,
        provider: SearchProvider | None = None,
        platform: str = "",
        timeout_seconds: float | None = None,
    ) -> None:
        event = asyncio.Event()
        self.waiters[job_id] = event
        if provider:
            self.waiting_contexts[job_id] = (provider, gate, platform)
        deadline = self.deadlines.get(job_id)
        when = deadline.when() if deadline else None
        remaining = max(1.0, when - asyncio.get_running_loop().time()) if when else None
        if deadline:
            deadline.reschedule(None)
        self.update(job_id, status=gate.state, error_message=str(gate))
        if timeout_seconds is None:
            timeout_seconds = getattr(settings, "browser_interaction_timeout", 1800)

        monitor_task: asyncio.Task[None] | None = None
        if provider and hasattr(provider, "browser"):
            poll_interval = 0.1 if timeout_seconds and timeout_seconds < 10.0 else 2.0

            async def _check_auto_resume() -> None:
                while not event.is_set():
                    try:
                        await asyncio.sleep(poll_interval)
                        browser = getattr(provider, "browser", None)
                        if browser and hasattr(browser, "inspect"):
                            page = None
                            if hasattr(browser, "get_active_page"):
                                try:
                                    page = browser.get_active_page()
                                except Exception:
                                    pass
                            if not page and hasattr(browser, "page"):
                                page = browser.page
                            if page and not getattr(page, "is_closed", lambda: True)():
                                current_state = await browser.inspect(page)
                                is_login_wait = gate.state == "waiting_for_login" or "login" in str(gate).lower()
                                if is_login_wait:
                                    is_auth = (
                                        current_state == "authenticated"
                                        or bool(getattr(browser, "diagnostics", {}).get("is_authenticated"))
                                    )
                                    if is_auth:
                                        logger.info(
                                            "Auto-resume: User authenticated on page %s for job %s",
                                            getattr(page, "url", ""),
                                            job_id,
                                        )
                                        event.set()
                                        break
                                else:
                                    # CAPTCHA / verification challenge wait
                                    if current_state in ("page_available", "authenticated"):
                                        captcha_visible = False
                                        if hasattr(page, "locator"):
                                            try:
                                                c_box = page.locator(
                                                    "#captcha-verify-image:visible, .captcha-verify-container:visible, [class*='captcha']:visible"
                                                )
                                                captcha_visible = (await c_box.count()) > 0
                                            except Exception:
                                                captcha_visible = False
                                        if not captcha_visible:
                                            logger.info(
                                                "Auto-resume: CAPTCHA / verification cleared on page %s for job %s",
                                                getattr(page, "url", ""),
                                                job_id,
                                            )
                                            event.set()
                                            break
                    except Exception:
                        pass

            monitor_task = asyncio.create_task(_check_auto_resume())

        try:
            async with asyncio.timeout(timeout_seconds):
                await event.wait()
            self.update(job_id, status="resuming", waiting_reason=None)
            self.update(job_id, status="searching", error_message=None)
        finally:
            if monitor_task and not monitor_task.done():
                monitor_task.cancel()
                try:
                    await monitor_task
                except asyncio.CancelledError:
                    pass
            self.waiting_contexts.pop(job_id, None)
            self.waiters.pop(job_id, None)
            if deadline:
                deadline.reschedule(
                    asyncio.get_running_loop().time() + (remaining or settings.search_timeout)
                )
