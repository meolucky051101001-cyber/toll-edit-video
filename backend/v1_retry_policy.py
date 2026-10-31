"""Bounded job retries only for recognizably transient failures (no AI imports)."""
import asyncio


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
    """True only for connection/DNS/timeout failures, never for content failures."""
    chain = _exception_chain(error)
    if any(_is_permanent_exception(exc) for exc in chain):
        return False
    for exc in chain:
        if type(exc).__name__ in NETWORK_ERROR_NAMES:
            return True
        message = str(exc).lower()
        if any(marker in message for marker in NETWORK_ERROR_MARKERS):
            return True
    return False
