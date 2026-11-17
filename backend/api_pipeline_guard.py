"""Cross-process lease for direct API media work; preserve work on client disconnect."""
import asyncio
import functools
import logging
import inspect
from pathlib import Path
import uuid
import job_tracker
from service_control import service_paused
from batch_control import stop_check
from v1_resource_control import begin_video, end_video
from fastapi import HTTPException

_tasks = set()


def has_active_media_tasks():
    return any(not task.done() for task in _tasks)

def guarded_media_api(function):
    signature = inspect.signature(function)
    @functools.wraps(function)
    async def wrapper(*args, **kwargs):
        async def run():
            lease = uuid.uuid4().hex
            if service_paused(Path(__file__).resolve().parent.parent, workspace=job_tracker.WORKSPACE,
                              service="processing"):
                raise HTTPException(409, "Tool đang tắt; chưa nhận xử lý video mới.")
            bound = signature.bind_partial(*args, **kwargs)
            source = bound.arguments.get("video_path")
            video_name = Path(source).name if isinstance(source, str) and source else "API media"
            try:
                # Claim before any await or artifact write. The shared file lease
                # arbitrates with Telegram and batch processes, not just HTTP tasks.
                job_tracker.start_batch(1, job_id=lease, exclusive=True)
            except job_tracker.JobAlreadyRunningError:
                raise HTTPException(409, "Pipeline đang được bot hoặc batch khác sử dụng.")
            execution_token = job_tracker.current_execution.set(lease)
            stop_token = stop_check.set(job_tracker.is_stop_requested)
            failure = None
            try:
                import shared_state
                # start_batch cleared only the previous job's persisted stop.
                # Reset the local latch without removing a newly arriving stop file.
                shared_state._local_stop = False
                job_tracker.start_video(video_name)
                begin_video(lease)
                result = await function(*args, **kwargs)
                if isinstance(result, dict) and result.get("status") == "error":
                    failure = str(result.get("message") or "Media API failed")
                return result
            except asyncio.CancelledError:
                job_tracker.request_stop()
                raise
            except BaseException as error:
                failure = str(error) or type(error).__name__
                raise
            finally:
                try:
                    summary = end_video()
                    logging.getLogger('v1.performance').info('API resource_summary=%s', summary)
                finally:
                    try:
                        if job_tracker.is_stop_requested():
                            job_tracker.mark_stopped("Tác vụ API đã dừng theo yêu cầu.")
                        elif failure:
                            job_tracker.fail_batch(failure, video_name=video_name, job_id=lease)
                        elif job_tracker.get_status().get("job_id") == lease:
                            job_tracker.finish_batch()
                    finally:
                        try:
                            job_tracker.release_batch(lease)
                        finally:
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
