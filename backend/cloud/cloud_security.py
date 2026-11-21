"""
cloud_security.py - Phase 1 security primitives for Tool V1 Cloud GPU pipeline.

Provides:
- Strict allowlists for GPU types, task types, TTS voices, URL schemes, and video extensions.
- Input & local file boundary validation against path traversal and option smuggling.
- Secret redaction for logs, exception messages, tracebacks, and metadata artifacts.
- Local control token resolution and Host / Origin / loopback validation matching V1 core.
"""

from __future__ import annotations

import logging
import os
import re
import secrets
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Optional, Sequence
from urllib.parse import urlsplit

from .cloud_config import BACKEND_DIR, DEFAULT_CONFIG, WORKSPACE_DIR, get_cloud_output_dir

logger = logging.getLogger("cloud.security")

ALLOWED_GPU_TYPES: frozenset[str] = frozenset(
    {"T4", "L4", "A100", "H100", "G4", "V5E1", "V6E1", "CPU"}
)
CUDA_GPU_TYPES: frozenset[str] = frozenset({"T4", "L4", "A100", "H100", "G4"})
ALLOWED_TASK_TYPES: frozenset[str] = frozenset({"full_pipeline"})
ALLOWED_VOICES: frozenset[str] = frozenset(
    {
        "auto",
        "BV562_streaming",
        "BV075_streaming",
        "BV421_vivn_streaming",
        "vi-VN-HoaiMyNeural",
        "vi-VN-NamMinhNeural",
    }
)
ALLOWED_URL_SCHEMES: frozenset[str] = frozenset({"http", "https"})
ALLOWED_VIDEO_EXTENSIONS: frozenset[str] = frozenset(
    {".mp4", ".mov", ".mkv", ".avi", ".webm", ".m4v", ".ts"}
)

SESSION_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{1,62}$")
_CONTROL_CHARS_RE = re.compile(r"[\x00-\x1f\x7f]")
_SCHEME_PREFIX_RE = re.compile(r"^([A-Za-z][A-Za-z0-9+.-]*):")

_LOOPBACK_HOSTS: frozenset[str] = frozenset(
    {"127.0.0.1", "localhost", "::1", "testserver", "testclient"}
)

_GOOGLE_API_KEY_RE = re.compile(r"AIza[0-9A-Za-z\-_]{20,}")
_BEARER_RE = re.compile(
    r"(?i)(authorization[\"']?\s*[:=]\s*[\"']?bearer\s+)[^\s\"',;]+"
)
_KV_SECRET_RE = re.compile(
    r"(?i)\b(gemini_api_key|api_key|apikey|access_token|refresh_token|bot_token|token|secret|password|key)"
    r"(\s*[:=]\s*[\"']?)([^\s&\"',;]+)"
)
_CLI_KEY_FLAG_RE = re.compile(
    r"(?i)(--gemini-key(?:=|\s+))([^\s\"';]+)"
)
_COOKIE_HEADER_RE = re.compile(
    r"(?im)((?:set-)?cookie\s*:\s*)([^\r\n]+)"
)
_COOKIE_KV_RE = re.compile(
    r"(?i)\b(sapisid|apisid|hsid|ssid|sidcc|osid|__secure-[a-z0-9_-]+)(\s*=\s*)([^\s&\"',;]+)"
)

_TELEGRAM_BOT_TOKEN_RE = re.compile(r"\b\d{6,15}:[A-Za-z0-9_-]{20,}\b")

_SECRET_ENV_NAMES: tuple[str, ...] = (
    "GEMINI_API_KEY",
    "GEMINI_API_KEYS",
    "TELEGRAM_BOT_TOKEN",
    "BOT_TOKEN",
    "CLOUD_BOT_TOKEN",
    "DOWNLOADER_BOT_TOKEN",
    "AUTODUB_LOCAL_TOKEN",
    "LOCAL_TOKEN",
    "OPENAI_API_KEY",
    "HF_TOKEN",
)


class CloudValidationError(ValueError):
    """Structured validation error carrying an HTTP status code and contract error code."""

    def __init__(self, message: str, *, code: str = "INVALID_INPUT", http_status: int = 400):
        super().__init__(message)
        self.code = code
        self.http_status = http_status


@dataclass(frozen=True)
class ValidatedVideoInput:
    """Normalized, verified Cloud video input descriptor."""

    kind: str  # "url" | "local_file"
    normalized_value: str
    display_name: str
    extension: str


def has_control_chars(value: str) -> bool:
    """Return True if value contains ASCII control characters (including NUL, CR, LF)."""
    return bool(_CONTROL_CHARS_RE.search(value))


def get_default_allowed_roots() -> list[Path]:
    """Return canonical allowed root directories for local video inputs."""
    candidates: list[Path] = [
        Path(WORKSPACE_DIR),
        Path(DEFAULT_CONFIG["results_dir"]).parent,
        Path(DEFAULT_CONFIG["results_dir"]),
        Path(BACKEND_DIR).parent / "workspace",
        get_cloud_output_dir(),
        Path(os.getenv("AUTODUB_OUTPUT_DIR", r"D:\banve")),
    ]
    env_workspace = os.getenv("AUTODUB_WORKSPACE", "").strip()
    if env_workspace:
        candidates.insert(0, Path(env_workspace))
    extra_raw = os.getenv("AUTODUB_CLOUD_ALLOWED_ROOTS", "").strip()
    if extra_raw:
        for item in extra_raw.split(os.pathsep):
            item = item.strip()
            if item:
                candidates.append(Path(item))

    resolved: list[Path] = []
    seen: set[str] = set()
    for path_item in candidates:
        try:
            res = path_item.expanduser().resolve(strict=False)
        except Exception:
            continue
        key = str(res).casefold()
        if key not in seen:
            seen.add(key)
            resolved.append(res)
    return resolved


def _normalize_roots(allowed_roots: Optional[Sequence[str | Path]]) -> list[Path]:
    raw_roots = list(allowed_roots) if allowed_roots is not None else get_default_allowed_roots()
    normalized: list[Path] = []
    for root in raw_roots:
        try:
            normalized.append(Path(root).expanduser().resolve(strict=False))
        except Exception:
            continue
    return normalized


def validate_gpu_type(gpu_type: Optional[str]) -> str:
    """Validate GPU/TPU/CPU accelerator type against the explicit allowlist."""
    val = str(gpu_type or "").strip().upper()
    if not val or has_control_chars(val) or val not in ALLOWED_GPU_TYPES:
        raise CloudValidationError(
            f"Loại GPU không hợp lệ: '{gpu_type}'. Cho phép: {', '.join(sorted(ALLOWED_GPU_TYPES))}.",
            code="UNSUPPORTED_GPU",
            http_status=400,
        )
    return val


def validate_video_gpu_type(gpu_type: Optional[str]) -> str:
    """Require an NVIDIA CUDA accelerator on new video submissions."""
    gpu = validate_gpu_type(gpu_type)
    if gpu not in CUDA_GPU_TYPES:
        raise CloudValidationError(
            f"Pipeline edit video cần GPU NVIDIA CUDA. {gpu} không khả dụng để gửi video.",
            code="UNSUPPORTED_GPU",
        )
    return gpu


def validate_task_type(task_type: Optional[str]) -> str:
    """Validate task type against the explicit allowlist."""
    val = str(task_type or "").strip()
    if not val or has_control_chars(val) or val not in ALLOWED_TASK_TYPES:
        raise CloudValidationError(
            f"Loại tác vụ không hợp lệ: '{task_type}'. Cho phép: {', '.join(sorted(ALLOWED_TASK_TYPES))}.",
            code="UNSUPPORTED_TASK",
            http_status=400,
        )
    return val


def validate_voice(voice: Optional[str]) -> str:
    """Validate TTS voice identifier against the explicit allowlist."""
    val = str(voice or "").strip()
    if not val or has_control_chars(val) or val.startswith("-") or val not in ALLOWED_VOICES:
        raise CloudValidationError(
            f"Giọng đọc TTS không hợp lệ: '{voice}'. Cho phép: {', '.join(sorted(ALLOWED_VOICES))}.",
            code="UNSUPPORTED_VOICE",
            http_status=400,
        )
    return val


def validate_session_name(session_name: Optional[str]) -> str:
    """Validate Colab session identifier strictly before calling WSL CLI."""
    val = str(session_name or "").strip()
    if not val or has_control_chars(val) or not SESSION_NAME_RE.fullmatch(val):
        raise CloudValidationError(
            f"Tên session Colab không hợp lệ: '{session_name}'.",
            code="INVALID_INPUT",
            http_status=400,
        )
    return val


def sanitize_remote_filename(filename: str, fallback: str = "input_video.mp4") -> str:
    """Produce a safe POSIX basename for remote /content storage."""
    base = Path(str(filename or "")).name
    if not base or has_control_chars(base):
        return fallback
    suffix = Path(base).suffix.lower()
    if suffix not in ALLOWED_VIDEO_EXTENSIONS:
        suffix = ".mp4"
    stem = re.sub(r"[^A-Za-z0-9._-]", "_", Path(base).stem).strip("._-")
    if not stem:
        stem = "input_video"
    return f"{stem[:80]}{suffix}"


def _looks_like_non_drive_scheme(raw: str) -> Optional[str]:
    """Return scheme string if input has a URI scheme other than a Windows drive letter."""
    if "://" in raw:
        return raw.split("://", 1)[0].strip().lower()
    m = _SCHEME_PREFIX_RE.match(raw)
    if m and len(m.group(1)) > 1:
        return m.group(1).lower()
    return None


def validate_video_input(
    raw_input: Optional[str],
    *,
    allowed_roots: Optional[Sequence[str | Path]] = None,
    require_exists: bool = True,
) -> ValidatedVideoInput:
    """
    Validate and normalize a Cloud job input (http/https URL or local video file).

    - Rejects control characters, disallowed URI schemes, directories, missing files,
      disallowed file extensions, leading-dash fake paths, and paths escaping allowed_roots.
    - Preserves benign filenames containing quotes, Unicode, spaces, or '&'.
    """
    if not isinstance(raw_input, str):
        raise CloudValidationError("Đường dẫn hoặc URL video phải là chuỗi hợp lệ.", code="INVALID_INPUT")

    cleaned = raw_input.strip()
    if not cleaned:
        raise CloudValidationError("Thiếu đường dẫn file hoặc link video URL.", code="INVALID_INPUT")
    if has_control_chars(cleaned):
        raise CloudValidationError("Đường dẫn hoặc URL chứa ký tự điều khiển không hợp lệ.", code="INVALID_INPUT")

    scheme = _looks_like_non_drive_scheme(cleaned)
    if scheme is not None:
        if scheme not in ALLOWED_URL_SCHEMES:
            raise CloudValidationError(
                f"URL scheme không được hỗ trợ: '{scheme}'. Chỉ cho phép http/https.",
                code="INVALID_INPUT",
                http_status=400,
            )
        if any(ch.isspace() for ch in cleaned) or any(ch in cleaned for ch in ('"', "'", "`", "<", ">")):
            raise CloudValidationError("URL video chứa khoảng trắng hoặc ký tự không an toàn.", code="INVALID_INPUT")
        parsed = urlsplit(cleaned)
        if not parsed.netloc or not parsed.hostname:
            raise CloudValidationError("URL video thiếu tên miền hợp lệ.", code="INVALID_INPUT")
        if parsed.username or parsed.password:
            raise CloudValidationError("Không cho phép nhúng thông tin xác thực trong URL.", code="INVALID_INPUT")

        host_raw = parsed.hostname.replace("www.", "").split(".")[0]
        host_part = re.sub(r"[^A-Za-z0-9_-]", "", host_raw) or "online"
        display_name = f"{host_part}_{uuid.uuid4().hex[:6]}.mp4"
        return ValidatedVideoInput(
            kind="url",
            normalized_value=cleaned,
            display_name=display_name,
            extension=".mp4",
        )

    if cleaned.startswith("-"):
        raise CloudValidationError("Đường dẫn file không được bắt đầu bằng dấu '-'.", code="INVALID_PATH")

    path_obj = Path(cleaned).expanduser()
    try:
        resolved = path_obj.resolve(strict=False)
    except Exception as exc:
        raise CloudValidationError(f"Đường dẫn file không hợp lệ: {exc}", code="INVALID_PATH") from exc

    roots = _normalize_roots(allowed_roots)
    if not roots or not any(resolved.is_relative_to(root) for root in roots):
        raise CloudValidationError(
            "Đường dẫn file video nằm ngoài vùng thư mục được phép xử lý.",
            code="INVALID_PATH",
            http_status=400,
        )

    if resolved.exists():
        if resolved.is_dir():
            raise CloudValidationError("Đầu vào phải là file video hợp lệ, không chấp nhận thư mục.", code="INVALID_PATH")
        if not resolved.is_file():
            raise CloudValidationError("Đầu vào không phải file thường hợp lệ.", code="INVALID_PATH")
    elif require_exists:
        raise CloudValidationError(
            f"File video không tồn tại trên máy: {resolved.name}",
            code="INVALID_PATH",
            http_status=404,
        )

    ext = resolved.suffix.lower()
    if ext not in ALLOWED_VIDEO_EXTENSIONS:
        raise CloudValidationError(
            f"Định dạng file '{ext or '<none>'}' không phải video được hỗ trợ.",
            code="INVALID_PATH",
            http_status=400,
        )

    return ValidatedVideoInput(
        kind="local_file",
        normalized_value=str(resolved),
        display_name=resolved.name,
        extension=ext,
    )


def resolve_cloud_bot_token() -> str:
    """Resolve dedicated Cloud Telegram Bot token from env or workspace secret file."""
    raw_env = os.getenv("CLOUD_BOT_TOKEN", "").strip().strip('"').strip("'")
    if raw_env and not raw_env.upper().startswith(("YOUR_", "PASTE_")):
        if _TELEGRAM_BOT_TOKEN_RE.fullmatch(raw_env):
            return raw_env

    workspace_root = Path(
        os.getenv("AUTODUB_WORKSPACE", str(BACKEND_DIR.parent / "workspace"))
    )
    token_file = workspace_root / "cloud_desktop" / ".cloud_bot_token"
    try:
        if not token_file.is_symlink() and token_file.is_file() and token_file.stat().st_size <= 512:
            val = token_file.read_text(encoding="utf-8").strip().strip('"').strip("'")
            if val and _TELEGRAM_BOT_TOKEN_RE.fullmatch(val):
                return val
    except OSError:
        pass
    return ""


def _collect_runtime_secrets(extra_secrets: Iterable[str] = ()) -> list[str]:
    values: list[str] = []
    for env_name in _SECRET_ENV_NAMES:
        raw = os.getenv(env_name, "").strip()
        if not raw:
            continue
        for part in re.split(r"[\s,;]+", raw):
            part = part.strip()
            if len(part) >= 6:
                values.append(part)
    cloud_bot_tok = resolve_cloud_bot_token()
    if cloud_bot_tok:
        values.append(cloud_bot_tok)
    for item in extra_secrets:
        if item and len(str(item).strip()) >= 6:
            values.append(str(item).strip())
    # Longest first to prevent partial substring leakage.
    return sorted(set(values), key=len, reverse=True)


def redact_secrets(text: Optional[str], extra_secrets: Iterable[str] = ()) -> str:
    """Scrub known API keys, tokens, and secret patterns before logging or persistence."""
    if not text:
        return ""
    scrubbed = str(text)
    for secret_val in _collect_runtime_secrets(extra_secrets):
        scrubbed = scrubbed.replace(secret_val, "[REDACTED]")
    scrubbed = _GOOGLE_API_KEY_RE.sub("[REDACTED_API_KEY]", scrubbed)
    scrubbed = _TELEGRAM_BOT_TOKEN_RE.sub("[REDACTED_BOT_TOKEN]", scrubbed)
    scrubbed = _BEARER_RE.sub(r"\1[REDACTED]", scrubbed)
    scrubbed = _COOKIE_HEADER_RE.sub(r"\1[REDACTED_COOKIE]", scrubbed)
    scrubbed = _COOKIE_KV_RE.sub(r"\1\2[REDACTED]", scrubbed)
    scrubbed = _CLI_KEY_FLAG_RE.sub(r"\1[REDACTED]", scrubbed)
    scrubbed = _KV_SECRET_RE.sub(r"\1\2[REDACTED]", scrubbed)
    return scrubbed


def resolve_local_control_token() -> str:
    """
    Resolve or initialize the shared local control token using the same search order as backend/main.py.
    """
    env_token = os.getenv("AUTODUB_LOCAL_TOKEN", "").strip()
    if env_token:
        return env_token

    workspace_root = Path(
        os.getenv(
            "AUTODUB_WORKSPACE",
            str(BACKEND_DIR.parent / "workspace"),
        )
    )
    candidate_paths = [
        workspace_root / "bot_system" / ".dashboard_control_token",
        workspace_root / ".dashboard_control_token",
        BACKEND_DIR.parent / "workspace_backup_safety" / ".dashboard_control_token",
        BACKEND_DIR / ".dashboard_control_token",
    ]
    for path_item in candidate_paths:
        try:
            if path_item.is_file():
                tok = path_item.read_text(encoding="utf-8").strip()
                if len(tok) >= 16:
                    return tok
        except Exception:
            continue

    new_token = secrets.token_urlsafe(32)
    for path_item in candidate_paths[:2]:
        try:
            path_item.parent.mkdir(parents=True, exist_ok=True)
            path_item.write_text(new_token, encoding="utf-8")
            break
        except Exception:
            continue
    return new_token


def is_valid_loopback_host(host_header: str) -> bool:
    """Validate Host header to mitigate DNS rebinding attacks."""
    if not host_header:
        return False
    h = host_header.strip().lower()
    if h.startswith("[") and "]" in h:
        host_part = h[1 : h.index("]")]
    else:
        host_part = h.split(":", 1)[0]
    return host_part in _LOOPBACK_HOSTS


def is_loopback_client(client_host: str) -> bool:
    """Check whether remote peer IP belongs to local loopback / TestClient."""
    return str(client_host or "").strip().lower() in _LOOPBACK_HOSTS


def is_valid_loopback_origin(origin_header: str) -> bool:
    """Validate browser Origin header when present on requests."""
    if not origin_header:
        return False
    cleaned = origin_header.strip()
    if cleaned.lower() == "null":
        return False
    try:
        parsed = urlsplit(cleaned)
    except Exception:
        return False
    if parsed.scheme.lower() not in ALLOWED_URL_SCHEMES:
        return False
    hostname = (parsed.hostname or "").strip().lower()
    return hostname in _LOOPBACK_HOSTS


def allowed_loopback_origins(ports: Iterable[int] = (8088, 8089, 8090)) -> list[str]:
    """Build explicit CORS origin allowlist for local UI dashboards."""
    origins: list[str] = []
    for port in ports:
        origins.append(f"http://127.0.0.1:{port}")
        origins.append(f"http://localhost:{port}")
        origins.append(f"http://[::1]:{port}")
    return origins
