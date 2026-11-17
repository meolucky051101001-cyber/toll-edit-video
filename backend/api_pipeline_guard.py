"""Cross-process lease for direct API media work; preserve work on client disconnect."""
import asyncio
import functools
import logging
import uuid
from pathlib import Path
import job_tracker
from service_control import service_paused
from batch_control import stop_check, drain_task
from v1_resource_control import begin_video, end_video
from fastapi import HTTPException

_tasks = set()

def guarded_media_api(function):
    @functools.wraps(function)
    async def wrapper(*args, **kwargs):
        async def run():
            lease = uuid.uuid4().hex
            if service_paused(Path(__file__).resolve().parent.parent, workspace=job_tracker.WORKSPACE,
                              service="processing"):
                raise HTTPException(409, "Tool đang tắt; chưa nhận xử lý video mới.")
            try:
                job_tracker.start_batch(1, job_id=lease)
            except job_tracker.JobAlreadyRunningError:
                raise HTTPException(409, "Pipeline đang được bot hoặc batch khác sử dụng.")
            execution_token = job_tracker.current_execution.set(lease)
            stop_token = stop_check.set(job_tracker.is_stop_requested)
            try:
                begin_video(lease)
                result = await function(*args, **kwargs)
                if job_tracker.is_stop_requested():
                    job_tracker.mark_stopped()
                else:
                    job_tracker.finish_batch()
                return result
            except asyncio.CancelledError:
                job_tracker.request_stop()
                job_tracker.mark_stopped()
                raise
            except Exception as error:
                job_tracker.fail_batch(str(error), job_id=lease)
                raise
            finally:
                try:
                    summary = end_video()
                    logging.getLogger('v1.performance').info('API resource_summary=%s', summary)
                finally:
                    job_tracker.release_batch(lease)
                    stop_check.reset(stop_token)
                    job_tracker.current_execution.reset(execution_token)
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
