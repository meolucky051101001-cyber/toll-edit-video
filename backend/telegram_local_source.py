"""Exact local-source identities for durable Telegram jobs; never infer by filename."""
import os
from pathlib import Path

VIDEO_SUFFIXES = {".mp4", ".mkv", ".mov", ".avi", ".webm", ".m4v", ".flv"}
SOURCE_HOLDS = {"missing_local_source", "conflicting_local_source", "unverified_local_source", "changed_local_source"}


class LocalSourceError(ValueError):
    def __init__(self, message, reason_code="missing_local_source"):
        super().__init__(message)
        self.reason_code = reason_code


def _resolve(job, *, allow_relative=False):
    state = job.get("resume_state") or {}
    values = [value for value in (job.get("video_path"), state.get("source_path")) if value]
    if not values:
        return None, "missing_local_source"
    if any(not isinstance(value, str) or (not allow_relative and not Path(value).is_absolute()) for value in values):
        return None, "missing_local_source"
    try:
        paths = [Path(value).resolve() for value in values]
    except (OSError, ValueError):
        return None, "missing_local_source"
    if len({os.path.normcase(str(path)) for path in paths}) != 1:
        return None, "conflicting_local_source"
    return paths[0], None


def capture_source(job):
    """New explicit input, or revalidation of the frozen source before processing."""
    from batch_plan_service import fingerprint
    path, error = _resolve(job, allow_relative=True)
    if error or path is None:
        raise LocalSourceError("Đường dẫn video local thiếu hoặc mâu thuẫn; hãy gửi lại nguồn đầy đủ.", error)
    if not path.is_file() or path.suffix.lower() not in VIDEO_SUFFIXES:
        raise LocalSourceError("Nguồn local phải là file video tồn tại.")
    try:
        measured = fingerprint(path)
    except OSError as exc:
        raise LocalSourceError("Không đọc được nguồn local; hãy kiểm tra lại đường dẫn.") from exc
    if measured["size"] <= 0:
        raise LocalSourceError("Nguồn local rỗng.")
    state = dict(job.get("resume_state") or {})
    expected = state.get("source_sha256")
    if expected and expected != measured["sha256"]:
        raise LocalSourceError("Nguồn local đã thay đổi; không chạy lại job đã đóng băng.", "changed_local_source")
    state.update(source_path=str(path), source_sha256=measured["sha256"])
    job.update(video_path=str(path), resume_state=state)
    return path


def normalize_restored(job):
    if job.get("type") != "local_file":
        return job
    result = dict(job, resume_state=dict(job.get("resume_state") or {}))
    path, error = _resolve(result)
    if path is not None:
        result["video_path"] = result["resume_state"]["source_path"] = str(path)
    if not error and not result["resume_state"].get("source_sha256"):
        error = "unverified_local_source"
    if not error:
        try:
            capture_source(result)
        except LocalSourceError as exc:
            error = exc.reason_code
    if error:
        result["hold"] = dict(result.get("hold") or {}, kind="manual", reason_code=error,
                              message="Nguồn local chưa xác minh. Bỏ job này và gửi lại đường dẫn đầy đủ.")
    return result
