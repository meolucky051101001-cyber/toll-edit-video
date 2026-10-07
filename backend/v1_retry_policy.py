"""Bounded job retries only for recognizably transient failures (no AI imports)."""
import asyncio
import subprocess


PERMANENT_ERROR_NAMES = {
    "SyntaxError",
    "ImportError",
    "FileNotFoundError",
    "SpeechTimingError",
    "CriticalQualityError",
    "GeminiAuthError",
    "BatchStopRequested",
    "UnsupportedResolutionError",
}


def should_retry_job(error, attempt=0, stopped=False, max_retries=2):
    if stopped or attempt >= max_retries:
        return False
    chain, seen = [], set()
    while error is not None and id(error) not in seen:
        seen.add(id(error))
        chain.append(error)
        error = error.__cause__ or error.__context__
    for exc in chain:
        message = str(exc).lower()
        if isinstance(exc, (asyncio.CancelledError, SyntaxError, ImportError, FileNotFoundError)):
            return False
        if "stop requested" in message or "operation cancelled" in message or "bị hủy" in message or "batchstoprequested" in message:
            return False
        if "unsupported filter" in message or "unsupported resolution" in message or "không được hỗ trợ" in message or "vượt quá giới hạn phần cứng" in message:
            return False
        if type(exc).__name__ in PERMANENT_ERROR_NAMES:
            return False
    return True
