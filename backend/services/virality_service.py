import logging
import math
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.core.events import EventBus
from backend.core.events import event_bus as default_event_bus
from backend.models.metric_snapshot import VideoMetricSnapshot
from backend.models.search_job import JobVideo
from backend.models.video import Video

logger = logging.getLogger("virality")


@dataclass(frozen=True)
class ViralityMetrics:
    hourly_velocity: float
    acceleration: float
    velocity_score: float
    virality_score: float
    spread_ratio: float
    age_bucket: str


def compute_virality(
    video: Any,
    snapshots: list[VideoMetricSnapshot] | None = None,
) -> ViralityMetrics:
    """
    Computes velocity, acceleration, spread ratio, and composite virality score (0-100)
    normalized by video age bucket.
    """
    now = datetime.now(timezone.utc)

    # 1. Determine age and bucket
    pub = getattr(video, "published_at", None)
    if pub:
        pub_utc = pub.replace(tzinfo=timezone.utc) if pub.tzinfo is None else pub
        age_hours = max(0.1, (now - pub_utc).total_seconds() / 3600.0)
    else:
        age_hours = 48.0  # Reasonable default when published_at is unavailable

    if age_hours < 24.0:
        age_bucket = "fresh"
        velocity_cap = 5000.0
    elif age_hours < 168.0:
        age_bucket = "trending"
        velocity_cap = 2500.0
    elif age_hours < 720.0:
        age_bucket = "established"
        velocity_cap = 1000.0
    else:
        age_bucket = "evergreen"
        velocity_cap = 500.0

    # 2. Hourly velocity calculation
    if snapshots and len(snapshots) >= 2:
        sorted_snaps = sorted(snapshots, key=lambda s: s.captured_at)
        s_prev = sorted_snaps[-2]
        s_curr = sorted_snaps[-1]

        t_prev = (
            s_prev.captured_at.replace(tzinfo=timezone.utc)
            if s_prev.captured_at.tzinfo is None
            else s_prev.captured_at
        )
        t_curr = (
            s_curr.captured_at.replace(tzinfo=timezone.utc)
            if s_curr.captured_at.tzinfo is None
            else s_curr.captured_at
        )

        dt_hours = max(0.02, (t_curr - t_prev).total_seconds() / 3600.0)
        delta_likes = (s_curr.like_count or 0) - (s_prev.like_count or 0)
        hourly_velocity = max(0.0, float(delta_likes) / dt_hours)
    else:
        likes = getattr(video, "like_count", 0) or 0
        hourly_velocity = max(0.0, float(likes) / age_hours)

    # 3. Acceleration calculation
    if snapshots and len(snapshots) >= 3:
        sorted_snaps = sorted(snapshots, key=lambda s: s.captured_at)
        s1, s2, s3 = sorted_snaps[-3], sorted_snaps[-2], sorted_snaps[-1]

        t1 = s1.captured_at.replace(tzinfo=timezone.utc) if s1.captured_at.tzinfo is None else s1.captured_at
        t2 = s2.captured_at.replace(tzinfo=timezone.utc) if s2.captured_at.tzinfo is None else s2.captured_at
        t3 = s3.captured_at.replace(tzinfo=timezone.utc) if s3.captured_at.tzinfo is None else s3.captured_at

        dt1 = max(0.02, (t2 - t1).total_seconds() / 3600.0)
        dt2 = max(0.02, (t3 - t2).total_seconds() / 3600.0)

        v1 = max(0.0, float((s2.like_count or 0) - (s1.like_count or 0)) / dt1)
        v2 = max(0.0, float((s3.like_count or 0) - (s2.like_count or 0)) / dt2)

        dt_mid = max(0.04, (t3 - t1).total_seconds() / 3600.0)
        acceleration = (v2 - v1) / dt_mid
    else:
        acceleration = 0.0

    # 4. Spread ratio (active comments, shares, favorites relative to views/likes)
    likes = max(getattr(video, "like_count", 0) or 0, 10)
    comments = getattr(video, "comment_count", 0) or 0
    shares = getattr(video, "share_count", 0) or 0
    favorites = getattr(video, "favorite_count", 0) or 0

    spread = (2.5 * shares + 1.5 * comments + 1.0 * favorites) / float(likes)
    spread_score = min(100.0, spread * 50.0)

    # 5. Velocity normalized score (0 - 100)
    v_norm = min(100.0, 100.0 * math.log1p(hourly_velocity) / math.log1p(velocity_cap))

    # 6. Acceleration bonus (0 - 15 points)
    accel_bonus = min(15.0, max(0.0, acceleration * 0.1)) if acceleration > 0 else 0.0

    # 7. Composite Virality Score
    raw_virality = 0.70 * v_norm + 0.20 * spread_score + accel_bonus
    virality_score = round(min(100.0, max(0.0, raw_virality)), 2)
    velocity_score = round(min(100.0, max(0.0, v_norm)), 2)

    return ViralityMetrics(
        hourly_velocity=round(hourly_velocity, 2),
        acceleration=round(acceleration, 2),
        velocity_score=velocity_score,
        virality_score=virality_score,
        spread_ratio=round(spread, 3),
        age_bucket=age_bucket,
    )


class ViralityService:
    def __init__(self, event_bus: EventBus | None = None):
        self.event_bus = event_bus if event_bus is not None else default_event_bus

    def compute_metrics(
        self,
        video: Any,
        snapshots: list[VideoMetricSnapshot] | None = None,
    ) -> ViralityMetrics:
        return compute_virality(video, snapshots=snapshots)

    def update_video_virality(self, video_id: str, db: Session) -> ViralityMetrics | None:
        video = db.get(Video, video_id)
        if not video:
            return None

        # Fetch snapshots
        stmt = (
            select(VideoMetricSnapshot)
            .where(VideoMetricSnapshot.video_id == video_id)
            .order_by(VideoMetricSnapshot.captured_at.asc())
        )
        snapshots = list(db.scalars(stmt).all())

        metrics = compute_virality(video, snapshots=snapshots)

        video.velocity_score = metrics.velocity_score
        video.virality_score = metrics.virality_score

        # Recalculate quality and final scores with new virality
        from backend.services.ranking_service import recalculate_scores_from_virality

        new_quality, new_final = recalculate_scores_from_virality(
            relevance=video.relevance_score,
            like_count=video.like_count,
            comment_count=video.comment_count,
            favorite_count=video.favorite_count,
            share_count=video.share_count,
            published_at=video.published_at,
            virality_score=metrics.virality_score,
        )
        video.quality_score = new_quality
        video.final_score = new_final

        # Update JobVideo links if any
        job_links = list(
            db.scalars(select(JobVideo).where(JobVideo.video_id == video_id)).all()
        )
        for link in job_links:
            link.velocity_score = metrics.velocity_score
            link.virality_score = metrics.virality_score
            link.quality_score = new_quality
            link.final_score = round(link.relevance_score * (0.8 + 0.2 * new_quality / 100.0), 2)

        db.commit()

        # Emit real-time event
        if self.event_bus:
            payload = {
                "video_id": video_id,
                "virality_score": metrics.virality_score,
                "velocity_score": metrics.velocity_score,
                "hourly_velocity": metrics.hourly_velocity,
                "acceleration": metrics.acceleration,
                "age_bucket": metrics.age_bucket,
                "spread_ratio": metrics.spread_ratio,
            }
            self.event_bus.publish_nowait("video:virality_update", payload)

        return metrics


def compute_engagement_velocity(video: Any) -> float:
    """Convenience helper returning the normalized velocity score (0-100)."""
    return compute_virality(video).velocity_score
