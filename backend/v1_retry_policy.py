"""Bounded job retries only for recognizably transient failures (no AI imports)."""
import asyncio
import re


PERMANENT_ERROR_NAMES = {
    "SyntaxError",
    "ImportError",
    "FileNotFoundError",
    "SpeechTimingError",
    "CriticalQualityError",
    "OCRGeometryError",
    "DownloadValidationError",
    "GeminiAuthError",
    "GeminiResponseError",
    "GeminiRequestError",
    "GeminiConfigError",
    "GeminiCancelledError",
    "BatchStopRequested",
    "UnsupportedResolutionError",
    "FrozenConfigError",
    "GlossaryConfigError",
    "AudioDurationError",
    "SegmentCommitError",
    "JobStateReadError",
    "PipelineStateError",
    "OutputVerificationError",
    "StaleExecutionError",
    "CloudRecoveryRequired",
}

NETWORK_ERROR_NAMES = {
    "ConnectError",
    "ConnectTimeout",
    "ConnectionError",
    "ConnectionResetError",
    "ConnectionAbortedError",
    "ConnectionRefusedError",
    "BrokenPipeError",
    "NetworkError",
    "ReadError",
    "ReadTimeout",
    "WriteError",
    "WriteTimeout",
    "PoolTimeout",
    "RemoteProtocolError",
    "RemoteDisconnected",
    "IncompleteRead",
    "MaxRetryError",
    "NewConnectionError",
    "gaierror",
    "Timeout",
    "TimeoutError",
}

NETWORK_ERROR_MARKERS = (
    "connecterror",
    "readerror",
    "writeerror",
    "connection reset",
    "connection aborted",
    "connection refused",
    "connection error",
    "connection timed out",
    "connect timeout",
    "read timeout",
    "read timed out",
    "pool timeout",
    "network is unreachable",
    "network is down",
    "temporary failure in name resolution",
    "name or service not known",
    "nodename nor servname",
    "getaddrinfo",
    "timed out",
    "timeout",
    "quá thời gian chờ",
    "hết thời gian chờ",
    "không thể kết nối",
    "lỗi kết nối",
    "kết nối api",
    "mất kết nối",
    "lỗi mạng",
)

PERMANENT_ERROR_MARKERS = (
    "strict translation mode requires a complete llm translation conforming to quality rules",
    "unsupported filter",
    "unsupported resolution",
    "không được hỗ trợ",
    "vượt quá giới hạn phần cứng",
    "unsupported url",
    "bài viết trên tiểu hồng thư",
    "album ảnh",
    "dính logo watermark",
    "không tìm thấy luồng video sạch",
    "downloaded file is missing or empty",
    "downloaded file has no supported video container",
    "downloaded container has no video stream",
    "downloaded video duration is invalid",
    "ffprobe rejected",
    "ffprobe returned invalid json",
    "downloaded duration",
    "unexpectedly small",
    "wrong byte size",
    "refusing incomplete video response",
    "http range request must return 206",
    "content-range",
)

TRANSIENT_HTTP_STATUSES = {408, 429, 500, 502, 503, 504}
GEMINI_TRANSIENT_MARKERS = (
    "timeout", "timed out", "connection", "overloaded", "unavailable",
    "resource_exhausted", "resource exhausted",
)


def _http_status(error):
    status = getattr(getattr(error, "response", None), "status_code", None)
    return status if isinstance(status, int) else None


def _gemini_permanent_reason(error):
    if type(error).__name__ != "GeminiAllModelsFailedError":
        return False
    message = str(error).lower()
    return bool(re.search(r"\b(?:http|status|code)\s*[=:]?\s*(?:400|401|403|404|422)\b", message)) or any(
        marker in message for marker in (
            "unauthenticated", "permission_denied", "invalid_argument",
            "invalid api key", "bad request",
        )
    )


def _exception_chain(error):
    chain, seen = [], set()
    while error is not None and id(error) not in seen:
        seen.add(id(error))
        chain.append(error)
        error = error.__cause__ or error.__context__
    return chain


def _is_permanent_exception(exc):
    if isinstance(exc, (asyncio.CancelledError, SyntaxError, ImportError, FileNotFoundError)):
        return True
    status = _http_status(exc)
    if status is not None and 400 <= status < 500 and status not in TRANSIENT_HTTP_STATUSES:
        return True
    if _gemini_permanent_reason(exc):
        return True
    message = str(exc).lower()
    if "stop requested" in message or "operation cancelled" in message or "bị hủy" in message or "batchstoprequested" in message:
        return True
    if any(marker in message for marker in PERMANENT_ERROR_MARKERS):
        return True
    return type(exc).__name__ in PERMANENT_ERROR_NAMES


def should_retry_job(error, attempt=0, stopped=False, max_retries=2):
    if stopped or attempt >= max_retries:
        return False
    return not any(_is_permanent_exception(exc) for exc in _exception_chain(error))


def is_transient_network_error(error):
    """Retry transport/upstream outages; permanent causes always take precedence."""
    chain = _exception_chain(error)
    if any(_is_permanent_exception(exc) for exc in chain):
        return False
    for exc in chain:
        name = type(exc).__name__
        if name in {"GeminiQuotaExhaustedError", "GeminiDeadlineError"}:
            return True
        if _http_status(exc) in TRANSIENT_HTTP_STATUSES:
            return True
        if name == "GeminiAllModelsFailedError":
            message = str(exc).lower()
            if re.search(r"(?<!\d)(?:429|500|502|503|504)(?!\d)", message) or any(
                marker in message for marker in GEMINI_TRANSIENT_MARKERS
            ):
                return True
        if type(exc).__name__ in NETWORK_ERROR_NAMES:
            return True
        message = str(exc).lower()
        if any(marker in message for marker in NETWORK_ERROR_MARKERS):
            return True
    return False
