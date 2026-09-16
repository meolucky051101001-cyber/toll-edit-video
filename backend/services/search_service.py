import asyncio
import inspect
import logging
import re
from collections.abc import Callable
from typing import cast

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from backend.core.config import settings
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
from backend.services.ranking_service import rank
from backend.services.translator import is_cjk, plan_query_async

TERMINAL = {"completed", "failed", "cancelled", "interrupted"}
logger = logging.getLogger("research")


class SearchService:
    def __init__(
        self,
        sessions: sessionmaker[Session],
        factory: Callable[[Platform, bool], SearchProvider] = get_provider,
        ai: AIService | None = None,
    ):
        self.sessions = sessions
        self.factory = factory
        self.ai = ai
        self.tasks: dict[str, asyncio.Task[None]] = {}
        self.lock = asyncio.Lock()
        self.platform_locks: dict[str, asyncio.Lock] = {}
        self.waiters: dict[str, asyncio.Event] = {}
        self.deadlines: dict[str, asyncio.Timeout] = {}

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
                for key, value in values.items():
                    setattr(job, key, value)
                db.commit()

    async def cancel(self, job_id: str) -> None:
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
        try:
            await self.pipeline(job_id)
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
        queries = await self.prepare_queries(job_id, query)
        if not use_mock:
            queries = queries[:3]
        for platform_index, platform in enumerate(platforms):
            provider_limit = limit // len(platforms) + (platform_index < limit % len(platforms))
            if provider_limit == 0:
                continue
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
                                if remaining <= 0:
                                    break
                                db.add(SearchQuery(job_id=job_id, platform=platform, query=term))
                                db.commit()
                            if use_mock:
                                query_limit = max(
                                    1, (remaining + len(queries) - index - 1) // (len(queries) - index)
                                )
                            else:
                                query_limit = remaining
                            self.update(job_id, status="searching")

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
                                            SearchFilters(),
                                            on_batch=on_batch,
                                        )
                                    else:
                                        results = await provider.search(
                                            term,
                                            query_limit,
                                            SearchFilters(),
                                        )
                                    break
                                except UserActionRequired as gate:
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
                                    await self.wait_for_user(job_id, gate)
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
                for result in validated_batch:
                    job.found_count += 1
                    existing = find_existing(db, result)
                    scores = rank(result, ranking_target, alternative_queries=alternative_queries)
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
                        existing = Video(
                            **values,
                            canonical_url=canon_u,
                            fingerprint=fingerprint(result),
                            search_query=query,
                            search_job_id=job_id,
                            relevance_score=scores[0],
                            quality_score=scores[1],
                            final_score=scores[2],
                        )
                        db.add(existing)
                        db.flush()
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
        event.set()
        return True

    def resume_all_waiting(self) -> int:
        count = 0
        for event in list(self.waiters.values()):
            event.set()
            count += 1
        return count

    async def wait_for_user(self, job_id: str, gate: UserActionRequired) -> None:
        event = asyncio.Event()
        self.waiters[job_id] = event
        deadline = self.deadlines.get(job_id)
        when = deadline.when() if deadline else None
        remaining = max(1.0, when - asyncio.get_running_loop().time()) if when else None
        if deadline:
            deadline.reschedule(None)
        self.update(job_id, status=gate.state, error_message=str(gate))
        try:
            async with asyncio.timeout(1800):
                await event.wait()
            self.update(job_id, status="searching", error_message=None)
        finally:
            self.waiters.pop(job_id, None)
            if deadline:
                deadline.reschedule(
                    asyncio.get_running_loop().time() + (remaining or settings.search_timeout)
                )
