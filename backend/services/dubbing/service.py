import asyncio
import logging
import os
import time
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from backend.core.config import ROOT, settings
from backend.core.events import EventBus, get_event_bus
from backend.models.download_job import DownloadJob
from backend.models.dubbing_job import DubbingJob
from backend.models.video import Video
from backend.schemas.dubbing import DubbingJobOut, DubbingRequest, VoiceOption
from backend.services.dubbing.audio_mixer import AudioMixer
from backend.services.dubbing.tts_adapter import (
    AVAILABLE_VOICES,
    BaseTTSAdapter,
    get_tts_adapter,
)

logger = logging.getLogger("dubbing.service")


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class DubbingService:
    def __init__(
        self,
        sessions: sessionmaker[Session],
        tts: BaseTTSAdapter | None = None,
        mixer: AudioMixer | None = None,
        event_bus: EventBus | None = None,
    ):
        self.sessions = sessions
        self.tts = tts or get_tts_adapter()
        self.mixer = mixer or AudioMixer()
        self.event_bus = event_bus or get_event_bus()
        self.running_tasks: dict[str, asyncio.Task] = {}
        self._active_jobs: set[str] = set()

    def list_voices(self) -> list[VoiceOption]:
        return AVAILABLE_VOICES

    def extract_script_from_video(self, video: Video) -> str:
        if video.script_analysis and isinstance(video.script_analysis, dict):
            remake = video.script_analysis.get("remake_script_vi")
            if isinstance(remake, dict):
                parts = []
                if remake.get("intro"):
                    parts.append(remake["intro"])
                if remake.get("body") and isinstance(remake["body"], list):
                    parts.extend(remake["body"])
                if remake.get("cta"):
                    parts.append(remake["cta"])
                if parts:
                    return "\n".join(parts)

        # Fallback to title and caption
        title = (video.title or "").strip()
        caption = (video.caption or "").strip()
        if title and caption:
            return f"{title}. {caption}"
        return title or caption or "Nội dung video ngắn xu hướng."

    def _create_job_record(self, req: DubbingRequest, db: Session) -> tuple[DubbingJob, DubbingJobOut]:
        video = db.get(Video, req.video_id)
        if not video:
            raise ValueError(f"Video {req.video_id} không tồn tại trong thư viện.")

        script_text = (req.script_text or "").strip()
        if not script_text:
            script_text = self.extract_script_from_video(video)

        job = DubbingJob(
            video_id=req.video_id,
            status="pending",
            voice_id=req.voice_id,
            script_text=script_text,
            speed=req.speed,
            pitch=req.pitch,
            bgm_volume_percent=req.bgm_volume_percent,
            ducking=req.ducking,
        )
        db.add(job)
        db.commit()
        db.refresh(job)
        return job, self.serialize_job(job)

    def create_job(self, req: DubbingRequest, db: Session) -> DubbingJobOut:
        job, out = self._create_job_record(req, db)
        try:
            loop = asyncio.get_running_loop()
            task = loop.create_task(self.process_dubbing(job.id))
            self.running_tasks[job.id] = task
        except RuntimeError:
            pass
        return out

    async def create_job_async(self, req: DubbingRequest, db: Session) -> DubbingJobOut:
        job, out = self._create_job_record(req, db)
        from backend.core.queue import system_queue
        try:
            await system_queue.enqueue(
                task_type="dubbing",
                payload={"job_id": job.id},
                priority=2,
            )
            logger.info("Enqueued dubbing job %s to system_queue", job.id)
        except Exception as q_exc:
            logger.warning("Queue enqueue skipped (%s); fallback to direct background task", q_exc)
            task = asyncio.create_task(self.process_dubbing(job.id))
            self.running_tasks[job.id] = task
        return out

    async def process_dubbing(self, job_id: str) -> None:
        if job_id in self._active_jobs:
            logger.info("Dubbing job %s is already active in-memory, skipping duplicate execution", job_id)
            return

        self._active_jobs.add(job_id)
        try:
            logger.info("Starting dubbing job %s", job_id)
            script_text = ""
            voice_id = "vi-VN-HoaiMyNeural"
            speed = 1.0
            pitch = 1.0
            bgm_vol = 15
            ducking = True
            video_id = ""

            with self.sessions() as db:
                job = db.get(DubbingJob, job_id)
                if not job:
                    logger.warning("Dubbing job %s not found in database", job_id)
                    return
                if job.status != "pending":
                    logger.info("Dubbing job %s status is '%s' (not pending), skipping duplicate execution", job_id, job.status)
                    return
                job.status = "synthesizing"
                db.commit()

                script_text = job.script_text
                voice_id = job.voice_id
                speed = job.speed
                pitch = job.pitch
                bgm_vol = job.bgm_volume_percent
                ducking = job.ducking
                video_id = job.video_id

            await self._emit_event(job_id, "synthesizing")

            out_audio_path = f"data/dubbing/{job_id}_{voice_id}.mp3"

            try:
                start_tts = time.perf_counter()
                audio_path, duration = await self.tts.synthesize(
                    text=script_text,
                    voice_id=voice_id,
                    speed=speed,
                    pitch=pitch,
                    output_path=out_audio_path,
                )
                tts_ms = (time.perf_counter() - start_tts) * 1000.0
                from backend.services.observability import telemetry_service
                telemetry_service.record_latency("dubbing_synthesis_ms", tts_ms)
                telemetry_service.record_counter("dubbing_completed", 1)

                self._update_job_status(job_id, "mixing")
                await self._emit_event(job_id, "mixing")

                # Check if source video file is downloaded locally
                source_video_path = None
                platform_video_id = None
                with self.sessions() as db:
                    video = db.get(Video, video_id)
                    if video:
                        platform_video_id = video.platform_video_id
                    dl_job = db.scalars(
                        select(DownloadJob)
                        .where(DownloadJob.video_id == video_id, DownloadJob.status == "completed")
                        .order_by(DownloadJob.created_at.desc())
                        .limit(1)
                    ).first()
                    if dl_job and dl_job.relative_path:
                        cand = dl_job.relative_path
                        candidates_to_check = [
                            settings.download_dir / cand,
                            ROOT / "data" / "downloads" / cand,
                            Path("data/downloads") / cand,
                            Path("data") / cand,
                            Path(cand),
                        ]
                        for p in candidates_to_check:
                            try:
                                p_res = p.resolve()
                                if p_res.is_file():
                                    source_video_path = str(p_res)
                                    break
                            except Exception:
                                pass

                if not source_video_path:
                    direct_candidates = [
                        settings.download_dir / f"{video_id}.mp4",
                        settings.download_dir / f"{video_id}.webm",
                        Path(f"data/downloads/{video_id}.mp4"),
                        Path(f"data/downloads/{video_id}.webm"),
                    ]
                    for dc in direct_candidates:
                        if dc.is_file():
                            source_video_path = str(dc.resolve())
                            break

                if not source_video_path:
                    search_dirs = [settings.download_dir, ROOT / "data" / "downloads", Path("data/downloads")]
                    for sdir in search_dirs:
                        if sdir.is_dir():
                            if platform_video_id:
                                matched = list(sdir.glob(f"*{platform_video_id}*.mp4"))
                                if matched and matched[0].is_file():
                                    source_video_path = str(matched[0].resolve())
                                    break
                            matched_vid = list(sdir.glob(f"*{video_id}*.mp4"))
                            if matched_vid and matched_vid[0].is_file():
                                source_video_path = str(matched_vid[0].resolve())
                                break

                final_audio, final_video = await self.mixer.mix(
                    voiceover_audio_path=audio_path,
                    source_video_path=source_video_path,
                    output_dir="data/dubbing",
                    bgm_volume_percent=bgm_vol,
                    ducking=ducking,
                )

                with self.sessions() as db:
                    job = db.get(DubbingJob, job_id)
                    if job:
                        job.status = "completed"
                        job.audio_path = final_audio
                        job.video_path = final_video
                        job.duration_seconds = duration
                        job.completed_at = utcnow()
                        db.commit()

                logger.info("Dubbing job %s completed successfully (%.2fs)", job_id, duration)
                await self._emit_event(job_id, "completed")

            except Exception as exc:
                logger.exception("Dubbing job %s failed: %s", job_id, exc)
                with self.sessions() as db:
                    job = db.get(DubbingJob, job_id)
                    if job:
                        job.status = "failed"
                        job.error_message = str(exc)
                        job.completed_at = utcnow()
                        db.commit()
                await self._emit_event(job_id, "failed", str(exc))
        finally:
            self._active_jobs.discard(job_id)
            self.running_tasks.pop(job_id, None)

    def _update_job_status(self, job_id: str, status: str) -> None:
        with self.sessions() as db:
            job = db.get(DubbingJob, job_id)
            if job:
                job.status = status
                db.commit()

    async def _emit_event(self, job_id: str, status: str, error: str | None = None) -> None:
        if self.event_bus:
            payload = {"job_id": job_id, "status": status}
            if error:
                payload["error"] = error
            await self.event_bus.publish("dubbing:update", payload)
            await self.event_bus.publish("dubbing", payload)

    def get_job(self, job_id: str, db: Session) -> DubbingJobOut | None:
        job = db.get(DubbingJob, job_id)
        return self.serialize_job(job) if job else None

    def get_latest_job_for_video(self, video_id: str, db: Session) -> DubbingJobOut | None:
        job = db.scalars(
            select(DubbingJob)
            .where(DubbingJob.video_id == video_id)
            .order_by(DubbingJob.created_at.desc())
            .limit(1)
        ).first()
        return self.serialize_job(job) if job else None

    def get_jobs_for_video(self, video_id: str, db: Session) -> list[DubbingJobOut]:
        jobs = db.scalars(
            select(DubbingJob)
            .where(DubbingJob.video_id == video_id)
            .order_by(DubbingJob.created_at.desc())
        ).all()
        return [self.serialize_job(j) for j in jobs]

    def serialize_job(self, job: DubbingJob) -> DubbingJobOut:
        audio_url = f"/api/dubbing/files/{os.path.basename(job.audio_path)}" if job.audio_path else None
        video_url = f"/api/dubbing/files/{os.path.basename(job.video_path)}" if job.video_path else None

        return DubbingJobOut(
            id=job.id,
            video_id=job.video_id,
            status=job.status,
            voice_id=job.voice_id,
            script_text=job.script_text,
            audio_url=audio_url,
            video_url=video_url,
            duration_seconds=job.duration_seconds,
            error_message=job.error_message,
            created_at=job.created_at.isoformat() if job.created_at else utcnow().isoformat(),
            completed_at=job.completed_at.isoformat() if job.completed_at else None,
        )
