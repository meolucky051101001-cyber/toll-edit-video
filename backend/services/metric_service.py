import logging
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from backend.core.events import EventBus
from backend.core.events import event_bus as default_event_bus
from backend.models.metric_snapshot import VideoMetricSnapshot
from backend.schemas.contracts import MetricHistoryOut, MetricSnapshotOut

logger = logging.getLogger("metrics")


class MetricService:
    def __init__(
        self,
        sessions: sessionmaker[Session] | None = None,
        event_bus: EventBus | None = None,
        throttle_seconds: int = 300,
    ):
        self.sessions = sessions
        self.event_bus = event_bus if event_bus is not None else default_event_bus
        self.throttle_seconds = throttle_seconds

    def record_snapshot(
        self,
        video_id: str,
        like_count: int | None = None,
        comment_count: int | None = None,
        share_count: int | None = None,
        favorite_count: int | None = None,
        view_count: int | None = None,
        source: str = "search",
        force: bool = False,
        db: Session | None = None,
    ) -> VideoMetricSnapshot | None:
        """
        Records a new metrics snapshot for the given video with deduplication and throttling.
        If identical metrics are reported within the throttle window and force=False, returns the last snapshot.
        """
        if db is not None:
            return self._record_snapshot_internal(
                db,
                video_id=video_id,
                like_count=like_count,
                comment_count=comment_count,
                share_count=share_count,
                favorite_count=favorite_count,
                view_count=view_count,
                source=source,
                force=force,
            )

        if self.sessions is None:
            raise RuntimeError("MetricService requires a database sessionmaker or an active session.")

        with self.sessions() as session:
            snapshot = self._record_snapshot_internal(
                session,
                video_id=video_id,
                like_count=like_count,
                comment_count=comment_count,
                share_count=share_count,
                favorite_count=favorite_count,
                view_count=view_count,
                source=source,
                force=force,
            )
            session.commit()
            if snapshot:
                session.refresh(snapshot)
            return snapshot

    def _record_snapshot_internal(
        self,
        db: Session,
        video_id: str,
        like_count: int | None,
        comment_count: int | None,
        share_count: int | None,
        favorite_count: int | None,
        view_count: int | None,
        source: str,
        force: bool,
    ) -> VideoMetricSnapshot:
        stmt = (
            select(VideoMetricSnapshot)
            .where(VideoMetricSnapshot.video_id == video_id)
            .order_by(VideoMetricSnapshot.captured_at.desc())
            .limit(1)
        )
        last_snapshot = db.execute(stmt).scalar_one_or_none()

        now = datetime.now(timezone.utc)

        if last_snapshot is not None and not force:
            same_metrics = (
                last_snapshot.like_count == like_count
                and last_snapshot.comment_count == comment_count
                and last_snapshot.share_count == share_count
                and last_snapshot.favorite_count == favorite_count
                and last_snapshot.view_count == view_count
            )
            last_time = (
                last_snapshot.captured_at.replace(tzinfo=timezone.utc)
                if last_snapshot.captured_at.tzinfo is None
                else last_snapshot.captured_at
            )
            elapsed = (now - last_time).total_seconds()
            if same_metrics and elapsed < self.throttle_seconds:
                logger.debug(
                    "Throttling snapshot for video %s (identical metrics, elapsed %ds < %ds)",
                    video_id,
                    int(elapsed),
                    self.throttle_seconds,
                )
                return last_snapshot

        snapshot = VideoMetricSnapshot(
            video_id=video_id,
            like_count=like_count,
            comment_count=comment_count,
            share_count=share_count,
            favorite_count=favorite_count,
            view_count=view_count,
            source=source,
            captured_at=now,
        )
        db.add(snapshot)
        db.flush()

        if self.event_bus:
            payload = {
                "video_id": video_id,
                "snapshot_id": snapshot.id,
                "like_count": snapshot.like_count,
                "comment_count": snapshot.comment_count,
                "share_count": snapshot.share_count,
                "favorite_count": snapshot.favorite_count,
                "view_count": snapshot.view_count,
                "captured_at": snapshot.captured_at.isoformat(),
                "source": snapshot.source,
            }
            self.event_bus.publish_nowait("video:metrics_update", payload)

        # Automatically cascade snapshot update to virality and ranking
        try:
            from backend.services.virality_service import ViralityService
            virality_svc = ViralityService(event_bus=self.event_bus)
            virality_svc.update_video_virality(video_id, db=db)
        except Exception as e:
            logger.debug("Auto virality update on snapshot skipped or failed: %s", e)

        return snapshot

    def get_history(
        self,
        video_id: str,
        limit: int = 100,
        db: Session | None = None,
    ) -> MetricHistoryOut:
        """
        Retrieves snapshot history with running deltas and elapsed seconds.
        """
        if db is not None:
            return self._get_history_internal(db, video_id, limit)

        if self.sessions is None:
            raise RuntimeError("MetricService requires a database sessionmaker or an active session.")

        with self.sessions() as session:
            return self._get_history_internal(session, video_id, limit)

    def _get_history_internal(self, db: Session, video_id: str, limit: int) -> MetricHistoryOut:
        stmt = (
            select(VideoMetricSnapshot)
            .where(VideoMetricSnapshot.video_id == video_id)
            .order_by(VideoMetricSnapshot.captured_at.asc())
            .limit(limit)
        )
        rows = list(db.scalars(stmt).all())

        results: list[MetricSnapshotOut] = []
        for i, row in enumerate(rows):
            captured_at = (
                row.captured_at.replace(tzinfo=timezone.utc)
                if row.captured_at.tzinfo is None
                else row.captured_at
            )

            delta_likes: int | None
            delta_comments: int | None
            delta_shares: int | None
            delta_favorites: int | None
            delta_views: int | None
            if i == 0:
                delta_likes = 0
                delta_comments = 0
                delta_shares = 0
                delta_favorites = 0
                delta_views = 0
                elapsed_seconds = 0.0
            else:
                prev = rows[i - 1]
                prev_time = (
                    prev.captured_at.replace(tzinfo=timezone.utc)
                    if prev.captured_at.tzinfo is None
                    else prev.captured_at
                )
                delta_likes = (
                    (row.like_count - prev.like_count)
                    if row.like_count is not None and prev.like_count is not None
                    else None
                )
                delta_comments = (
                    (row.comment_count - prev.comment_count)
                    if row.comment_count is not None and prev.comment_count is not None
                    else None
                )
                delta_shares = (
                    (row.share_count - prev.share_count)
                    if row.share_count is not None and prev.share_count is not None
                    else None
                )
                delta_favorites = (
                    (row.favorite_count - prev.favorite_count)
                    if row.favorite_count is not None and prev.favorite_count is not None
                    else None
                )
                delta_views = (
                    (row.view_count - prev.view_count)
                    if row.view_count is not None and prev.view_count is not None
                    else None
                )
                elapsed_seconds = max(0.0, (captured_at - prev_time).total_seconds())

            item = MetricSnapshotOut(
                id=row.id,
                video_id=row.video_id,
                like_count=row.like_count,
                comment_count=row.comment_count,
                share_count=row.share_count,
                favorite_count=row.favorite_count,
                view_count=row.view_count,
                captured_at=captured_at,
                source=row.source,
                delta_likes=delta_likes,
                delta_comments=delta_comments,
                delta_shares=delta_shares,
                delta_favorites=delta_favorites,
                delta_views=delta_views,
                elapsed_seconds=elapsed_seconds,
            )
            results.append(item)

        return MetricHistoryOut(
            video_id=video_id,
            total_snapshots=len(results),
            snapshots=results,
        )
