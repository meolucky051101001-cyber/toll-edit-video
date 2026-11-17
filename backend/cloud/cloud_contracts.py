"""Cloud execution contracts shared by the worker and Telegram entry point."""
from dataclasses import dataclass
from typing import Optional
from urllib.parse import urlsplit
import re
try:
    from ..v1_integrity_errors import OutputVerificationError
except ImportError:
    from v1_integrity_errors import OutputVerificationError


class CloudCapabilityError(ValueError):
    """The installed CLI/secret transport has not passed acceptance."""


class CloudVerificationError(OutputVerificationError):
    """Do not automatically rerender a paid job after artifact verification fails."""


class CloudOwnershipError(ValueError):
    pass


class CloudCancelled(Exception):
    pass


@dataclass(frozen=True)
class CloudRunResult:
    status: str
    job_id: str
    attempt_id: Optional[str] = None
    result_path: Optional[str] = None
    receipt: Optional[dict] = None
    error: Optional[str] = None

    def __bool__(self):
        return self.status == "completed" and bool(self.receipt)


def validate_request(video_path, gpu_type, task_type, voice):
    if not isinstance(video_path, str) or not video_path or "\0" in video_path:
        raise ValueError("invalid_cloud_source")
    if "://" in video_path:
        parsed = urlsplit(video_path)
        if parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username or parsed.password:
            raise ValueError("invalid_cloud_url")
    if gpu_type.upper() not in {"T4", "L4", "A100", "H100", "G4", "CPU", "V5E1", "V6E1"}:
        raise ValueError("invalid_cloud_gpu")
    if task_type != "full_pipeline":
        raise ValueError("invalid_cloud_task")
    if not isinstance(voice, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", voice):
        raise ValueError("invalid_cloud_voice")


def redact(value, secrets=()):
    text = str(value)
    for secret in secrets:
        if secret:
            text = text.replace(secret, "[REDACTED]")
    return text
