"""Cross-process lease for direct API media work; preserve work on client disconnect."""
import asyncio
import functools
import logging
import uuid
import job_tracker
from v1_resource_control import begin_video, end_video
from fastapi import HTTPException

_tasks = set()

def guarded_media_api(function):
    @functools.wraps(function)
    async def wrapper(*args, **kwargs):
        async def run():
            lease = uuid.uuid4().hex
            try:
                with job_tracker._LOCK:
                    job_tracker._claim_batch_lock_unlocked(lease)
            except job_tracker.JobAlreadyRunningError:
                raise HTTPException(409, "Pipeline đang được bot hoặc batch khác sử dụng.")
            try:
                begin_video(lease)
                return await function(*args, **kwargs)
            finally:
                try:
                    summary = end_video()
                    logging.getLogger('v1.performance').info('API resource_summary=%s', summary)
                finally:
                    job_tracker.release_batch(lease)
        task = asyncio.create_task(run())
        _tasks.add(task)
        def done(completed):
            _tasks.discard(completed)
            if not completed.cancelled():
                # Retrieve detached errors so disconnects never leave unhandled tasks.
                error = completed.exception()
                if error and not isinstance(error, HTTPException):
                    logging.getLogger(__name__).error("Media API failed: %s", type(error).__name__)
        task.add_done_callback(done)
        return await asyncio.shield(task)
    return wrapper
