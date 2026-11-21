"""
colab_bridge.py - Hardened Colab CLI provider adapter via WSL2 for Tool V1.

Phase 1 & 2 guarantees:
- Strict validation of session names and accelerator types before invoking WSL/Colab CLI.
- Argv-based WSL invocations without shell interpolation (`shell=False`).
- `exec_argv` runs commands inside the remote Colab runtime with `shell=False` and passes
  secrets via session-scoped ephemeral env files (`0o600`), never via CLI flags.
- Interruptible subprocess execution (`cancel_check` + session-scoped process tracking),
  strictly job-scoped `stop_session`, and verified stop checks (`verify_session_stopped`).
- Automatic secret redaction across logs and error outputs.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import posixpath
import re
import shlex
import stat
import subprocess
import tempfile
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Set, Tuple

from .cloud_config import DEFAULT_CONFIG, to_wsl_path
from .cloud_security import (
    ALLOWED_VIDEO_EXTENSIONS,
    CloudValidationError,
    has_control_chars,
    redact_secrets,
    validate_gpu_type,
    validate_session_name,
)

logger = logging.getLogger("cloud.colab_bridge")

_ENV_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_DISALLOWED_SHELL_META = frozenset({";", "|", "&&", "||", ">", ">>", "<", "$(", "`"})
_WSL_DEFAULT_PATH = "/root/.local/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
_ORIG_SUBPROCESS_RUN = subprocess.run

_PROC_LOCK = threading.Lock()
_SESSION_CANCEL_EVENTS: Dict[str, threading.Event] = {}
_SESSION_PROCS: Dict[str, Set[subprocess.Popen]] = {}

_TRANSIENT_TRANSPORT_MARKERS = (
    "connection was lost",
    "readerror",
    "connecterror",
    "remoteprotocolerror",
    "connectionreseterror",
    "serverdisconnectederror",
    "broken pipe",
    "connection aborted",
    "timeout sau",
)
_AUTODUB_LAUNCH_MARKER = "AUTODUB_LAUNCH_JSON:"
_AUTODUB_POLL_MARKER = "AUTODUB_POLL_JSON:"
_UPLOAD_CHUNK_BYTES = 8 * 1024 * 1024
_UPLOAD_BODY_REJECTED = re.compile(r"\b(?:400 Client Error|413 Client Error|413 (?:Payload|Content) Too Large)\b", re.I)
_TRANSFER_FAILURE_RE = re.compile(r"^\[colab\]\s+(?:upload|download)\s+failed\b", re.I | re.M)
_ASSEMBLE_VIDEO = """
import hashlib
from pathlib import Path
import shutil
import sys

target = Path(sys.argv[1])
prefix, part_count, size, digest = sys.argv[2:]
parts = [Path(f"{prefix}_{index}") for index in range(int(part_count))]
pending = Path(f"{prefix}.part")
try:
    with pending.open("xb") as out:
        for part in parts:
            with part.open("rb") as source:
                shutil.copyfileobj(source, out)
    with pending.open("rb") as assembled:
        hasher = hashlib.sha256()
        for chunk in iter(lambda: assembled.read(1 << 20), b""):
            hasher.update(chunk)
        checksum = hasher.hexdigest()
    if pending.stat().st_size != int(size) or checksum != digest:
        raise ValueError("Transferred video size or SHA256 does not match the original")
    pending.replace(target)
finally:
    pending.unlink(missing_ok=True)
    for part in parts:
        part.unlink(missing_ok=True)
"""


def _is_transient_transport_error(text: str) -> bool:
    low = str(text or "").lower()
    return any(marker in low for marker in _TRANSIENT_TRANSPORT_MARKERS)


def _wait_interruptible(
    seconds: float,
    *,
    session_name: Optional[str] = None,
    cancel_check: Optional[Callable[[], bool]] = None,
) -> bool:
    """Sleep up to `seconds`, returning True early if cancelled/aborted."""
    deadline = time.monotonic() + max(0.0, float(seconds))
    while time.monotonic() < deadline:
        if _is_session_aborted(session_name) or (cancel_check is not None and cancel_check()):
            return True
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        time.sleep(min(0.2, remaining))
    return _is_session_aborted(session_name) or bool(cancel_check is not None and cancel_check())


def _wsl_colab_bin() -> str:
    return str(DEFAULT_CONFIG.get("wsl_colab_bin", "/root/.local/bin/colab"))


def _get_or_create_cancel_event(session_name: str) -> threading.Event:
    with _PROC_LOCK:
        ev = _SESSION_CANCEL_EVENTS.get(session_name)
        if ev is None:
            if len(_SESSION_CANCEL_EVENTS) >= 512:
                inactive_keys = [k for k in _SESSION_CANCEL_EVENTS if k not in _SESSION_PROCS]
                for k in inactive_keys[:128]:
                    _SESSION_CANCEL_EVENTS.pop(k, None)
            ev = threading.Event()
            _SESSION_CANCEL_EVENTS[session_name] = ev
        return ev


def _is_session_aborted(session_name: Optional[str]) -> bool:
    if not session_name:
        return False
    with _PROC_LOCK:
        ev = _SESSION_CANCEL_EVENTS.get(session_name)
        return bool(ev and ev.is_set())


def clear_session_abort(session_name: str) -> None:
    """Clear an in-memory abort flag for a session name."""
    with _PROC_LOCK:
        _SESSION_CANCEL_EVENTS.pop(session_name, None)


def _terminate_proc(proc: subprocess.Popen) -> None:
    try:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=0.3)
            except Exception:
                if proc.poll() is None:
                    proc.kill()
    except Exception:
        pass


def abort_session_processes(session_name: str) -> int:
    """Signal cancellation and terminate active local WSL/Colab subprocesses for `session_name`."""
    safe_session = validate_session_name(session_name)
    with _PROC_LOCK:
        ev = _SESSION_CANCEL_EVENTS.get(safe_session)
        if ev is None:
            ev = threading.Event()
            _SESSION_CANCEL_EVENTS[safe_session] = ev
        ev.set()
        procs = list(_SESSION_PROCS.get(safe_session, ()))

    terminated = 0
    for proc in procs:
        try:
            was_running = proc.poll() is None
            _terminate_proc(proc)
            if was_running:
                terminated += 1
        except Exception:
            pass
    if terminated:
        logger.info("Đã ngắt %d tiến trình WSL cục bộ của session %s.", terminated, safe_session)
    return terminated


def _read_stream_bytes(stream: Any, bucket: List[bytes]) -> None:
    if stream is None:
        return
    try:
        data = stream.read()
        if data:
            if isinstance(data, str):
                bucket.append(data.encode("utf-8", errors="replace"))
            else:
                bucket.append(bytes(data))
    except Exception:
        pass


def _run_wsl_argv(
    program: str,
    args: Sequence[str] = (),
    *,
    timeout: int = 300,
    distro: Optional[str] = None,
    user: Optional[str] = None,
    stdin_text: Optional[str] = None,
    stdin: Any = None,
    extra_secrets: Sequence[str] = (),
    cancel_check: Optional[Callable[[], bool]] = None,
    session_name: Optional[str] = None,
    log_failure_output: bool = True,
) -> Tuple[int, str, str]:
    """Execute a binary inside WSL2 via strict argv list (never bash -c)."""
    target_distro = distro or DEFAULT_CONFIG["wsl_distro"]
    target_user = user or DEFAULT_CONFIG["wsl_user"]

    cmd_args = [
        "wsl",
        "-d",
        str(target_distro),
        "-u",
        str(target_user),
        "--exec",
        str(program),
        *[str(a) for a in args],
    ]

    use_popen = (cancel_check is not None) or (
        session_name is not None and subprocess.run is _ORIG_SUBPROCESS_RUN
    )

    if not use_popen:
        try:
            run_kwargs: Dict[str, Any] = {
                "capture_output": True,
                "timeout": timeout,
                "shell": False,
            }
            if stdin_text is not None:
                run_kwargs["input"] = stdin_text.encode("utf-8")
            elif stdin is not None:
                run_kwargs["stdin"] = stdin
            proc = subprocess.run(cmd_args, **run_kwargs)
            stdout = redact_secrets(
                proc.stdout.decode("utf-8", errors="replace").strip(),
                extra_secrets=extra_secrets,
            )
            stderr = redact_secrets(
                proc.stderr.decode("utf-8", errors="replace").strip(),
                extra_secrets=extra_secrets,
            )

            if proc.returncode != 0:
                if log_failure_output:
                    logger.warning(
                        "WSL argv failed (rc=%d): %s | stderr: %s",
                        proc.returncode,
                        program,
                        stderr[:300],
                    )
                else:
                    logger.warning("WSL argv failed (rc=%d): %s", proc.returncode, program)
            return proc.returncode, stdout, stderr

        except subprocess.TimeoutExpired:
            logger.error("WSL command timed out (%ds): %s", timeout, program)
            return -1, "", f"Timeout sau {timeout} giây."
        except Exception as exc:
            safe_exc = redact_secrets(str(exc), extra_secrets=extra_secrets)
            logger.error("WSL invocation exception: %s", safe_exc)
            return -2, "", safe_exc

    # Interruptible Popen path (Phase 2)
    if _is_session_aborted(session_name) or (cancel_check is not None and cancel_check()):
        return -3, "", "Tác vụ đã bị hủy bởi yêu cầu dừng."

    cancel_ev = _get_or_create_cancel_event(session_name) if session_name else None
    popen_obj: Optional[subprocess.Popen] = None
    out_chunks: List[bytes] = []
    err_chunks: List[bytes] = []
    t_out: Optional[threading.Thread] = None
    t_err: Optional[threading.Thread] = None
    cancelled = False
    timed_out = False

    try:
        popen_obj = subprocess.Popen(
            cmd_args,
            stdin=subprocess.PIPE if stdin_text is not None else stdin,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            shell=False,
        )
        if session_name:
            with _PROC_LOCK:
                _SESSION_PROCS.setdefault(session_name, set()).add(popen_obj)

        if stdin_text is not None and getattr(popen_obj, "stdin", None) is not None:
            try:
                popen_obj.stdin.write(stdin_text.encode("utf-8"))
                popen_obj.stdin.flush()
            except Exception:
                pass
            finally:
                try:
                    popen_obj.stdin.close()
                except Exception:
                    pass

        if getattr(popen_obj, "stdout", None) is not None:
            t_out = threading.Thread(
                target=_read_stream_bytes,
                args=(popen_obj.stdout, out_chunks),
                daemon=True,
            )
            t_out.start()
        if getattr(popen_obj, "stderr", None) is not None:
            t_err = threading.Thread(
                target=_read_stream_bytes,
                args=(popen_obj.stderr, err_chunks),
                daemon=True,
            )
            t_err.start()

        deadline = time.monotonic() + max(0.1, float(timeout))
        while True:
            if _is_session_aborted(session_name) or (cancel_check is not None and cancel_check()):
                cancelled = True
                _terminate_proc(popen_obj)
                break
            rc = popen_obj.poll()
            if rc is not None:
                break
            if time.monotonic() >= deadline:
                timed_out = True
                _terminate_proc(popen_obj)
                break
            if cancel_ev is not None:
                cancel_ev.wait(0.1)
            else:
                time.sleep(0.1)

        if not cancelled and (_is_session_aborted(session_name) or (cancel_check is not None and cancel_check())):
            cancelled = True

        if t_out is not None:
            t_out.join(timeout=2.0)
        if t_err is not None:
            t_err.join(timeout=2.0)

        if popen_obj.poll() is None:
            try:
                popen_obj.wait(timeout=2.0)
            except Exception:
                pass

        stdout = redact_secrets(
            b"".join(out_chunks).decode("utf-8", errors="replace").strip(),
            extra_secrets=extra_secrets,
        )
        stderr = redact_secrets(
            b"".join(err_chunks).decode("utf-8", errors="replace").strip(),
            extra_secrets=extra_secrets,
        )

        if cancelled:
            logger.info("WSL argv cancelled: %s (session=%s)", program, session_name or "-")
            return -3, stdout, stderr or "Tác vụ đã bị hủy bởi yêu cầu dừng."
        if timed_out:
            logger.error("WSL command timed out (%ds): %s", timeout, program)
            return -1, "", f"Timeout sau {timeout} giây."

        rc = popen_obj.returncode if popen_obj.returncode is not None else -2
        if rc != 0:
            if log_failure_output:
                logger.warning(
                    "WSL argv failed (rc=%d): %s | stderr: %s",
                    rc,
                    program,
                    stderr[:300],
                )
            else:
                logger.warning("WSL argv failed (rc=%d): %s", rc, program)
        return rc, stdout, stderr

    except Exception as exc:
        safe_exc = redact_secrets(str(exc), extra_secrets=extra_secrets)
        logger.error("WSL invocation exception: %s", safe_exc)
        return -2, "", safe_exc
    finally:
        if session_name and popen_obj is not None:
            with _PROC_LOCK:
                bucket = _SESSION_PROCS.get(session_name)
                if bucket is not None:
                    bucket.discard(popen_obj)
                    if not bucket:
                        _SESSION_PROCS.pop(session_name, None)
        for stream_name in ("stdout", "stderr", "stdin"):
            if popen_obj is not None:
                stream = getattr(popen_obj, stream_name, None)
                if stream is not None:
                    try:
                        stream.close()
                    except Exception:
                        pass


def _run_wsl_colab_argv(
    colab_args: Sequence[str],
    *,
    timeout: int = 300,
    distro: Optional[str] = None,
    user: Optional[str] = None,
    stdin: Any = None,
    stdin_text: Optional[str] = None,
    extra_secrets: Sequence[str] = (),
    cancel_check: Optional[Callable[[], bool]] = None,
    session_name: Optional[str] = None,
    log_failure_output: bool = True,
) -> Tuple[int, str, str]:
    """Execute Colab CLI in WSL2 via `env` + strict argv without bash interpolation."""
    env_args = [
        f"PATH={_WSL_DEFAULT_PATH}",
        "PYTHON_KEYRING_BACKEND=keyrings.alt.file.PlaintextKeyring",
        _wsl_colab_bin(),
        "--auth",
        "oauth2",
        *[str(a) for a in colab_args],
    ]
    return _run_wsl_argv(
        "env",
        env_args,
        timeout=timeout,
        distro=distro,
        user=user,
        stdin=stdin,
        stdin_text=stdin_text,
        extra_secrets=extra_secrets,
        cancel_check=cancel_check,
        session_name=session_name,
        log_failure_output=log_failure_output,
    )


def mount_drive(
    session_name: str,
    mount_path: str = "/content/drive",
    *,
    timeout: int = 180,
    cancel_check: Optional[Callable[[], bool]] = None,
) -> Tuple[bool, str]:
    raw_path = str(mount_path)
    path_parts = raw_path.split("/")
    normalized_path = posixpath.normpath(raw_path)
    if (
        has_control_chars(raw_path)
        or not raw_path.startswith("/")
        or ".." in path_parts
        or normalized_path == "/content"
        or not normalized_path.startswith("/content/")
    ):
        return False, "Đường dẫn gắn Google Drive không hợp lệ."
    try:
        safe_session = validate_session_name(session_name)
    except CloudValidationError:
        return False, "Tên session Colab không hợp lệ."

    rc, stdout, stderr = _run_wsl_colab_argv(
        ["drivemount", "-s", safe_session, normalized_path],
        timeout=max(1, int(timeout)),
        stdin=subprocess.DEVNULL,
        cancel_check=cancel_check,
        session_name=safe_session,
        log_failure_output=False,
    )
    output = f"{stdout}\n{stderr}"
    if "[colab] REQUIRED: Google Drive Authorization needed." in output:
        return (
            False,
            "Google Drive cần được ủy quyền một lần. Hãy chạy `colab drivemount` tương tác trong WSL rồi hoàn tất cấp quyền Google.",
        )
    if rc == 0:
        return True, "Đã gắn Google Drive."
    if rc == -3:
        return False, "Lệnh gắn Google Drive đã bị hủy."
    return False, f"Không thể gắn Google Drive qua Colab CLI (mã lỗi {rc})."


def _write_wsl_temp_script(wsl_path: str, content: str) -> None:
    """Write a temporary helper script inside WSL using stdin with 0600 mode (no command-line payload)."""
    writer_code = (
        "import os, sys\n"
        "path = sys.argv[1]\n"
        "data = sys.stdin.buffer.read()\n"
        "flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC\n"
        "fd = os.open(path, flags, 0o600)\n"
        "try:\n"
        "    os.chmod(path, 0o600)\n"
        "    os.write(fd, data)\n"
        "finally:\n"
        "    os.close(fd)\n"
    )
    rc, _, stderr = _run_wsl_argv(
        "python3",
        ["-c", writer_code, wsl_path],
        timeout=15,
        stdin_text=content,
    )
    if rc != 0:
        raise RuntimeError(f"Không thể ghi script tạm trong WSL ({wsl_path}): {stderr}")


def _remove_wsl_temp_file(wsl_path: str) -> None:
    try:
        _run_wsl_argv("rm", ["-f", wsl_path], timeout=10)
    except Exception:
        pass


def get_usage() -> Dict[str, Any]:
    """Query Colab Compute Units balance, burn rate, and active assignments."""
    rc, stdout, stderr = _run_wsl_colab_argv(["usage"], timeout=30)
    data = {
        "balance": 0.0,
        "usage_rate": 0.0,
        "active_assignments": 0,
        "raw": stdout or stderr,
        "ok": rc == 0,
    }
    if rc != 0:
        return data

    balance_m = re.search(r"Current balance:\s*([\d\.]+)", stdout)
    if balance_m:
        data["balance"] = float(balance_m.group(1))

    rate_m = re.search(r"Usage rate:\s*([\d\.]+)", stdout)
    if rate_m:
        data["usage_rate"] = float(rate_m.group(1))

    active_m = re.search(r"Active assignments:\s*(\d+)", stdout)
    if active_m:
        data["active_assignments"] = int(active_m.group(1))

    return data


def _parse_sessions_output(stdout: str) -> List[Dict[str, Any]]:
    if not stdout or "No active sessions found" in stdout or "No active sessions" in stdout:
        return []

    sessions: List[Dict[str, Any]] = []
    for line in stdout.splitlines():
        line_clean = line.strip()
        if not line_clean or line_clean.startswith("[colab]") or "Active sessions" in line_clean:
            continue
        left_part = line_clean.split("|")[0].strip()
        tokens = left_part.split()
        if not tokens:
            continue
        # Colab prints "[local-alias] provider-runtime-id | Hardware: ...".
        # Lifecycle ownership uses the alias passed to `new -s`, not the runtime ID.
        alias_match = re.match(r"^\[([A-Za-z0-9][A-Za-z0-9_-]*)\]\s+", left_part)
        s_name = alias_match.group(1) if alias_match else tokens[-1]
        is_active = not ("[?]" in tokens or "[-]" in tokens)
        sessions.append(
            {
                "name": s_name,
                "raw": line_clean,
                "is_active": is_active,
            }
        )
    return sessions


def get_sessions_status() -> Dict[str, Any]:
    """Query active Colab sessions and distinguish provider connectivity errors from empty list."""
    rc, stdout, stderr = _run_wsl_colab_argv(["sessions"], timeout=30)
    combined = redact_secrets(f"{stdout}\n{stderr}".strip())
    if rc != 0:
        return {
            "ok": False,
            "sessions": [],
            "error": combined or f"Colab CLI exited with code {rc}",
            "raw": combined,
        }
    return {
        "ok": True,
        "sessions": _parse_sessions_output(stdout),
        "error": "",
        "raw": stdout,
    }


def get_sessions() -> List[Dict[str, Any]]:
    """List currently active Colab sessions."""
    status = get_sessions_status()
    if not status.get("ok"):
        return []
    return list(status.get("sessions") or [])


def create_session(
    session_name: str,
    gpu: str = "T4",
    *,
    cancel_check: Optional[Callable[[], bool]] = None,
) -> Tuple[bool, str]:
    """Create a new Colab session using allowlisted accelerator types."""
    safe_session = validate_session_name(session_name)
    raw_acc = validate_gpu_type(gpu)

    if _is_session_aborted(safe_session) or (cancel_check is not None and cancel_check()):
        return False, "Tác vụ đã bị hủy trước khi tạo session."

    args: List[str] = ["new", "-s", safe_session]
    if raw_acc in ("V5E1", "V6E1"):
        tpu_type = "v6e1" if raw_acc == "V6E1" else "v5e1"
        args.extend(["--tpu", tpu_type])
        target_desc = f"TPU {tpu_type.upper()}"
    elif raw_acc == "CPU":
        target_desc = "CPU"
    else:
        args.extend(["--gpu", raw_acc])
        target_desc = f"GPU {raw_acc}"

    logger.info("Đang tạo session Colab: %s (%s)...", safe_session, target_desc)
    rc, stdout, stderr = _run_wsl_colab_argv(
        args,
        timeout=120,
        cancel_check=cancel_check,
        session_name=safe_session,
    )
    combined = redact_secrets(f"{stdout}\n{stderr}".strip())
    if rc == 0:
        logger.info("Tạo session %s thành công.", safe_session)
        return True, combined or "Tạo session thành công."
    if rc == -3:
        return False, f"Tác vụ đã bị hủy bởi yêu cầu dừng. {combined}".strip() if combined else "Tác vụ đã bị hủy bởi yêu cầu dừng."

    if "503" in combined or "unavailable" in combined.lower() or "no machines" in combined.lower():
        msg = f"Hết máy {target_desc} trên Colab (503 Server Error). Hãy thử lại sau hoặc chuyển sang GPU khác."
        logger.warning(msg)
        return False, msg

    return False, combined or "Không thể tạo session."


def _upload_with_retry(
    session_name: str,
    local_path: Path,
    remote_dest: str,
    *,
    timeout: int,
    cancel_check: Optional[Callable[[], bool]],
) -> Tuple[bool, str]:
    combined = ""
    for attempt_idx in range(1, 3):
        rc, stdout, stderr = _run_wsl_colab_argv(
            ["upload", "-s", session_name, to_wsl_path(local_path), remote_dest],
            timeout=timeout,
            cancel_check=cancel_check,
            session_name=session_name,
        )
        combined = redact_secrets(f"{stdout}\n{stderr}".strip())
        if rc == 0 and not _TRANSFER_FAILURE_RE.search(combined):
            return True, "Upload thành công."
        if rc == -3 or _is_session_aborted(session_name):
            return False, "Tác vụ đã bị hủy bởi yêu cầu dừng."
        if attempt_idx == 2 or not _is_transient_transport_error(combined):
            break
        logger.warning("Upload %s gặp rớt kết nối tạm; tự thử lại.", local_path.name)
        if _wait_interruptible(1.5, session_name=session_name, cancel_check=cancel_check):
            return False, "Tác vụ đã bị hủy bởi yêu cầu dừng."
    return False, combined or "Lỗi upload."


def _upload_video_in_parts(
    session_name: str,
    local_path: Path,
    filename: str,
    *,
    size: int,
    timeout: int,
    cancel_check: Optional[Callable[[], bool]],
) -> Tuple[bool, str]:
    prefix = f".autodub_upload_{uuid.uuid4().hex}"
    digest = hashlib.sha256()
    sent_bytes = part_count = 0
    chunk_size = min(_UPLOAD_CHUNK_BYTES, (size + 1) // 2)
    deadline = time.monotonic() + timeout
    try:
        with tempfile.TemporaryDirectory(prefix="autodub_upload_") as scratch, local_path.open("rb") as source:
            part_file = Path(scratch) / "upload.bin"
            for chunk in iter(lambda: source.read(chunk_size), b""):
                if _is_session_aborted(session_name) or (cancel_check and cancel_check()):
                    return False, "Tác vụ đã bị hủy trong lúc tải từng phần."
                remaining = int(deadline - time.monotonic())
                if remaining <= 0:
                    return False, "Hết thời gian tải video theo từng phần."
                sent_bytes += len(chunk)
                if sent_bytes > size:
                    return False, "Kích thước video nguồn đã thay đổi khi đang tải."
                digest.update(chunk)
                part_file.write_bytes(chunk)
                ok, detail = _upload_with_retry(
                    session_name, part_file, f"content/{prefix}_{part_count}",
                    timeout=remaining, cancel_check=cancel_check,
                )
                if not ok:
                    return False, f"Tải phần {part_count + 1} thất bại: {detail}"
                part_count += 1
    except OSError as exc:
        return False, f"Không đọc được dữ liệu video để tải từng phần: {redact_secrets(str(exc))}"
    if sent_bytes != size:
        return False, "Kích thước video nguồn đã thay đổi khi đang tải."
    remaining = int(deadline - time.monotonic())
    if remaining <= 0:
        return False, "Hết thời gian tải video theo từng phần."
    return exec_argv(
        session_name,
        ["python3", "-c", f"exec({_ASSEMBLE_VIDEO!r})",
         filename, prefix, str(part_count), str(size), digest.hexdigest()],
        cwd="/content", timeout=remaining, cancel_check=cancel_check,
    )


def upload_file(
    session_name: str,
    local_path: str | Path,
    remote_filename: Optional[str] = None,
    *,
    cancel_check: Optional[Callable[[], bool]] = None,
) -> Tuple[bool, str]:
    """Upload a local regular file to `/content/...` on the specified Colab session."""
    safe_session = validate_session_name(session_name)
    if _is_session_aborted(safe_session) or (cancel_check is not None and cancel_check()):
        return False, "Tác vụ đã bị hủy trước khi upload."

    path_obj = Path(local_path)
    if not path_obj.is_file():
        return False, f"File không tồn tại trên máy: {local_path}"

    remote_base = remote_filename or path_obj.name
    if has_control_chars(remote_base) or ".." in Path(remote_base).parts:
        raise CloudValidationError("Tên file đích trên Colab không hợp lệ.", code="INVALID_INPUT")
    remote_dest = remote_base if remote_base.startswith("content/") else f"content/{remote_base}"
    timeout = int(DEFAULT_CONFIG["upload_timeout_seconds"])

    logger.info("Đang upload %s lên session %s (remote: %s)...", path_obj.name, safe_session, remote_dest)
    filename = remote_dest.removeprefix("content/")
    ok, detail = _upload_with_retry(
        safe_session, path_obj, remote_dest, timeout=timeout, cancel_check=cancel_check,
    )
    if (
        not ok
        and _UPLOAD_BODY_REJECTED.search(detail)
        and re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.-]*", filename)
        and Path(filename).suffix.lower() in ALLOWED_VIDEO_EXTENSIONS
    ):
        size = path_obj.stat().st_size
        if size > 1:
            logger.warning("Colab từ chối tải trọn video (%d bytes); thử chuyển từng phần.", size)
            ok, part_detail = _upload_video_in_parts(
                safe_session, path_obj, filename, size=size, timeout=timeout, cancel_check=cancel_check,
            )
            if not ok:
                return False, f"{detail}\nTải video theo từng phần ({size} bytes) thất bại: {part_detail}"
            detail = "Đã tải từng phần và xác minh SHA256 của video."
    if ok:
        logger.info("Upload %s thành công.", path_obj.name)
    return ok, detail


def exec_script(
    session_name: str,
    script_local_path: str | Path,
    timeout: Optional[int] = None,
    *,
    cancel_check: Optional[Callable[[], bool]] = None,
) -> Tuple[bool, str]:
    """Execute a local Python script on the Colab runtime using `colab exec -f`."""
    safe_session = validate_session_name(session_name)
    if _is_session_aborted(safe_session) or (cancel_check is not None and cancel_check()):
        return False, "Tác vụ đã bị hủy trước khi chạy script."

    path_obj = Path(script_local_path)
    if not path_obj.is_file():
        return False, f"File script không tồn tại: {script_local_path}"

    wsl_script = to_wsl_path(path_obj)
    wait_time = int(timeout or DEFAULT_CONFIG["exec_timeout_seconds"])

    logger.info("Đang chạy script %s trên session %s...", path_obj.name, safe_session)
    rc, stdout, stderr = _run_wsl_colab_argv(
        ["exec", "-s", safe_session, "-f", wsl_script],
        timeout=wait_time,
        cancel_check=cancel_check,
        session_name=safe_session,
    )
    combined = redact_secrets(f"{stdout}\n{stderr}".strip())
    if rc == -3:
        return False, f"Tác vụ đã bị hủy bởi yêu cầu dừng. {combined}".strip() if combined else "Tác vụ đã bị hủy bởi yêu cầu dừng."
    is_ok = (rc == 0) and ("EXEC_CMD_FAILED_WITH_CODE:" not in combined)
    return is_ok, combined


def _validate_argv(argv: Sequence[str]) -> List[str]:
    if isinstance(argv, (str, bytes)) or not isinstance(argv, Sequence) or len(argv) == 0:
        raise CloudValidationError("argv phải là danh sách chuỗi không rỗng.", code="INVALID_INPUT")
    validated: List[str] = []
    for item in argv:
        if not isinstance(item, str) or not item:
            raise CloudValidationError("Mỗi phần tử trong argv phải là chuỗi không rỗng.", code="INVALID_INPUT")
        if has_control_chars(item):
            raise CloudValidationError("argv chứa ký tự điều khiển không hợp lệ.", code="INVALID_INPUT")
        validated.append(item)
    return validated


def _validate_env_vars(env_vars: Optional[Mapping[str, str]]) -> Dict[str, str]:
    if not env_vars:
        return {}
    clean: Dict[str, str] = {}
    for key, val in env_vars.items():
        if not isinstance(key, str) or not _ENV_KEY_RE.fullmatch(key):
            raise CloudValidationError(f"Tên biến môi trường không hợp lệ: {key!r}", code="INVALID_INPUT")
        if not isinstance(val, str) or "\x00" in val or "\r" in val or "\n" in val:
            raise CloudValidationError(f"Giá trị biến môi trường {key} chứa ký tự điều khiển.", code="INVALID_INPUT")
        clean[key] = val
    return clean


def _build_remote_argv_runner(
    argv: Sequence[str],
    *,
    cwd: str = "/content",
    remote_env_path: Optional[str] = None,
) -> str:
    """Build remote Python runner code that executes argv with `shell=False` and removes ephemeral env files."""
    return (
        "import json, os, subprocess, sys\n"
        f"argv = {list(argv)!r}\n"
        f"cwd = {cwd!r}\n"
        f"env_path = {remote_env_path!r}\n"
        "merged_env = os.environ.copy()\n"
        "try:\n"
        "    if env_path:\n"
        "        try:\n"
        "            os.chmod(env_path, 0o600)\n"
        "        except Exception:\n"
        "            pass\n"
        "        with open(env_path, 'r', encoding='utf-8') as ef:\n"
        "            loaded = json.load(ef)\n"
        "        if isinstance(loaded, dict):\n"
        "            for k, v in loaded.items():\n"
        "                if isinstance(k, str) and isinstance(v, str):\n"
        "                    merged_env[k] = v\n"
        "        try:\n"
        "            os.unlink(env_path)\n"
        "        except Exception:\n"
        "            pass\n"
        "    p = subprocess.Popen(argv, shell=False, cwd=cwd, env=merged_env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)\n"
        "    if p.stdout:\n"
        "        for line in p.stdout:\n"
        "            sys.stdout.write(line)\n"
        "            sys.stdout.flush()\n"
        "    p.wait()\n"
        "    if p.returncode != 0:\n"
        "        print('EXEC_CMD_FAILED_WITH_CODE:', p.returncode)\n"
        "        sys.exit(p.returncode)\n"
        "finally:\n"
        "    if env_path:\n"
        "        try:\n"
        "            if os.path.exists(env_path):\n"
        "                os.unlink(env_path)\n"
        "        except Exception:\n"
        "            pass\n"
    )


def exec_argv(
    session_name: str,
    argv: Sequence[str],
    *,
    cwd: str = "/content",
    env_vars: Optional[Mapping[str, str]] = None,
    timeout: Optional[int] = None,
    cancel_check: Optional[Callable[[], bool]] = None,
) -> Tuple[bool, str]:
    """
    Execute an explicit `argv` command on the remote Colab VM (`shell=False`).

    Any `env_vars` (e.g., `GEMINI_API_KEY`) are delivered via a session-scoped `0o600`
    ephemeral JSON file uploaded to `/content` and unlinked immediately upon loading,
    never embedded in CLI flags, shell commands, or runner source code.
    """
    safe_session = validate_session_name(session_name)
    safe_argv = _validate_argv(argv)
    if not isinstance(cwd, str) or not cwd or has_control_chars(cwd):
        raise CloudValidationError("Thư mục làm việc cwd trên Colab không hợp lệ.", code="INVALID_INPUT")
    safe_env = _validate_env_vars(env_vars)

    if _is_session_aborted(safe_session) or (cancel_check is not None and cancel_check()):
        return False, "Tác vụ đã bị hủy trước khi thực thi lệnh."

    wait_time = int(timeout or DEFAULT_CONFIG["exec_timeout_seconds"])
    local_env_file: Optional[Path] = None
    remote_env_path: Optional[str] = None
    secret_values = list(safe_env.values())

    try:
        if safe_env:
            run_id = uuid.uuid4().hex[:10]
            clean_s = re.sub(r"[^A-Za-z0-9_.-]", "", safe_session)
            env_name = f".colab_env_{clean_s}_{run_id}.json"
            fd, raw_tmp = tempfile.mkstemp(prefix=f"colab_env_{clean_s}_", suffix=".json")
            local_env_file = Path(raw_tmp)
            try:
                os.chmod(raw_tmp, stat.S_IRUSR | stat.S_IWUSR)
            except Exception:
                pass
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(safe_env, handle, ensure_ascii=False)
            if cancel_check is not None:
                up_ok, up_msg = upload_file(
                    safe_session,
                    local_env_file,
                    env_name,
                    cancel_check=cancel_check,
                )
            else:
                up_ok, up_msg = upload_file(safe_session, local_env_file, env_name)
            if not up_ok:
                return False, redact_secrets(f"Không thể nạp cấu hình phiên tạm: {up_msg}", extra_secrets=secret_values)
            remote_env_path = f"/content/{env_name}"

        if _is_session_aborted(safe_session) or (cancel_check is not None and cancel_check()):
            return False, "Tác vụ đã bị hủy bởi yêu cầu dừng."

        py_wrapper = _build_remote_argv_runner(
            safe_argv,
            cwd=cwd,
            remote_env_path=remote_env_path,
        )
        logger.info("Đang thực thi argv trên session %s: %s (args=%d)...", safe_session, safe_argv[0], len(safe_argv) - 1)
        rc, stdout, stderr = _run_wsl_colab_argv(
            ["exec", "-s", safe_session, "--timeout", str(wait_time)],
            timeout=wait_time + 5,
            stdin_text=py_wrapper,
            extra_secrets=secret_values,
            cancel_check=cancel_check,
            session_name=safe_session,
        )
        combined = redact_secrets(f"{stdout}\n{stderr}".strip(), extra_secrets=secret_values)
        if rc == -3:
            return False, f"Tác vụ đã bị hủy bởi yêu cầu dừng. {combined}".strip() if combined else "Tác vụ đã bị hủy bởi yêu cầu dừng."
        is_ok = (rc == 0) and ("EXEC_CMD_FAILED_WITH_CODE:" not in combined)
        return is_ok, combined

    finally:
        if local_env_file is not None:
            try:
                local_env_file.unlink(missing_ok=True)
            except Exception:
                pass


def _build_detached_launcher_script(
    argv: Sequence[str],
    *,
    cwd: str,
    run_id: str,
    safe_tag: str,
    remote_env_path: Optional[str],
    pid_file: str,
    status_file: str,
    log_file: str,
    progress_file: str,
) -> str:
    sup_script_path = f"/content/.autodub_sup_{safe_tag}_{run_id}.py"
    sup_code = "\n".join(
        [
            "import json, os, subprocess, sys, time",
            f"argv = {list(argv)!r}",
            f"cwd = {cwd!r}",
            f"run_id = {run_id!r}",
            f"env_path = {remote_env_path!r}",
            f"pid_file = {pid_file!r}",
            f"status_file = {status_file!r}",
            f"log_file = {log_file!r}",
            f"self_path = {sup_script_path!r}",
            "try:",
            "    if os.path.exists(self_path):",
            "        os.unlink(self_path)",
            "except Exception:",
            "    pass",
            "merged_env = os.environ.copy()",
            "if env_path and os.path.exists(env_path):",
            "    try:",
            "        with open(env_path, 'r', encoding='utf-8') as ef:",
            "            loaded = json.load(ef)",
            "        if isinstance(loaded, dict):",
            "            for k, v in loaded.items():",
            "                if isinstance(k, str) and isinstance(v, str):",
            "                    merged_env[k] = v",
            "    finally:",
            "        try:",
            "            os.unlink(env_path)",
            "        except Exception:",
            "            pass",
            "rc = 1",
            "log_fd = os.open(log_file, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)",
            "log_fh = os.fdopen(log_fd, 'w', encoding='utf-8', buffering=1)",
            "try:",
            "    child = subprocess.Popen(",
            "        argv, shell=False, cwd=cwd, env=merged_env,",
            "        stdin=subprocess.DEVNULL, stdout=log_fh, stderr=subprocess.STDOUT,",
            "        start_new_session=True, close_fds=True,",
            "    )",
            "    tmp_pid = pid_file + '.tmp'",
            "    with open(tmp_pid, 'w', encoding='utf-8') as pf:",
            "        json.dump({'run_id': run_id, 'sup_pid': os.getpid(), 'child_pid': child.pid, 'started_at': time.time()}, pf)",
            "    os.replace(tmp_pid, pid_file)",
            "    rc = int(child.wait())",
            "    if rc != 0:",
            "        log_fh.write(f'\\nEXEC_CMD_FAILED_WITH_CODE: {rc}\\n')",
            "        log_fh.flush()",
            "except Exception as exc:",
            "    log_fh.write(f'\\nSUPERVISOR_ERROR: {exc}\\nEXEC_CMD_FAILED_WITH_CODE: 1\\n')",
            "    log_fh.flush()",
            "    rc = 1",
            "finally:",
            "    try:",
            "        log_fh.close()",
            "    except Exception:",
            "        pass",
            "    tmp_st = status_file + '.tmp'",
            "    with open(tmp_st, 'w', encoding='utf-8') as sf:",
            "        json.dump({'run_id': run_id, 'done': True, 'returncode': rc, 'finished_at': time.time()}, sf)",
            "    os.replace(tmp_st, status_file)",
        ]
    )
    clear_meta = safe_tag == "pipeline"
    return "\n".join(
        [
            "import json, os, subprocess, sys, time",
            f"run_id = {run_id!r}",
            f"cwd = {cwd!r}",
            f"pid_file = {pid_file!r}",
            f"status_file = {status_file!r}",
            f"log_file = {log_file!r}",
            f"progress_file = {progress_file!r}",
            f"sup_script_path = {sup_script_path!r}",
            f"sup_code = {sup_code!r}",
            f"clear_meta = {clear_meta!r}",
            f"marker = {_AUTODUB_LAUNCH_MARKER!r}",
            "if os.path.isfile(pid_file):",
            "    try:",
            "        existing = json.loads(open(pid_file, 'r', encoding='utf-8').read())",
            "        if isinstance(existing, dict) and existing.get('run_id') == run_id:",
            "            print(marker + json.dumps({'launched': True, 'already_running': True}))",
            "            sys.exit(0)",
            "    except Exception:",
            "        pass",
            "stale_paths = [status_file, log_file, progress_file]",
            "if clear_meta:",
            "    stale_paths.append(os.path.join(cwd, 'result_metadata.json'))",
            "for sp in stale_paths:",
            "    try:",
            "        if os.path.exists(sp):",
            "            os.unlink(sp)",
            "    except Exception:",
            "        pass",
            "fd = os.open(sup_script_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)",
            "with os.fdopen(fd, 'w', encoding='utf-8') as sf:",
            "    sf.write(sup_code)",
            "proc = subprocess.Popen(",
            "    [sys.executable, '-u', sup_script_path],",
            "    shell=False, cwd=cwd,",
            "    stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,",
            "    start_new_session=True, close_fds=True,",
            ")",
            "if not os.path.exists(pid_file):",
            "    tmp_p = pid_file + '.init'",
            "    with open(tmp_p, 'w', encoding='utf-8') as pf:",
            "        json.dump({'run_id': run_id, 'sup_pid': proc.pid, 'child_pid': None, 'started_at': time.time()}, pf)",
            "    if not os.path.exists(pid_file):",
            "        os.replace(tmp_p, pid_file)",
            "    else:",
            "        try: os.unlink(tmp_p)",
            "        except Exception: pass",
            "time.sleep(0.2)",
            "print(marker + json.dumps({'launched': True, 'sup_pid': proc.pid}))",
        ]
    )


def _build_detached_poller_script(
    *,
    cwd: str,
    run_id: str,
    safe_tag: str,
    pid_file: str,
    status_file: str,
    log_file: str,
    progress_file: str,
) -> str:
    check_meta = safe_tag == "pipeline"
    return "\n".join(
        [
            "import json, os, time",
            f"cwd = {cwd!r}",
            f"run_id = {run_id!r}",
            f"pid_file = {pid_file!r}",
            f"status_file = {status_file!r}",
            f"log_file = {log_file!r}",
            f"progress_file = {progress_file!r}",
            f"check_meta = {check_meta!r}",
            f"marker = {_AUTODUB_POLL_MARKER!r}",
            "def _pid_alive(p):",
            "    if not isinstance(p, int) or p <= 1:",
            "        return False",
            "    try:",
            "        os.kill(p, 0)",
            "        return True",
            "    except ProcessLookupError:",
            "        return False",
            "    except PermissionError:",
            "        return True",
            "    except Exception:",
            "        return False",
            "progress = None",
            "if os.path.isfile(progress_file):",
            "    try:",
            "        cand = json.loads(open(progress_file, 'r', encoding='utf-8').read())",
            "        if isinstance(cand, dict):",
            "            progress = cand",
            "    except Exception:",
            "        pass",
            "done = False",
            "rc_val = None",
            "def _read_status():",
            "    if not os.path.isfile(status_file):",
            "        return None",
            "    try:",
            "        st = json.loads(open(status_file, 'r', encoding='utf-8').read())",
            "        if isinstance(st, dict) and st.get('run_id') == run_id and st.get('done'):",
            "            return st",
            "    except Exception:",
            "        pass",
            "    return None",
            "st_obj = _read_status()",
            "if st_obj is not None:",
            "    done = True",
            "    rc_val = int(st_obj.get('returncode', 1))",
            "elif os.path.isfile(pid_file):",
            "    try:",
            "        pinfo = json.loads(open(pid_file, 'r', encoding='utf-8').read())",
            "        if isinstance(pinfo, dict) and pinfo.get('run_id') == run_id:",
            "            started_at = float(pinfo.get('started_at') or 0.0)",
            "            sup_alive = _pid_alive(pinfo.get('sup_pid'))",
            "            child_alive = _pid_alive(pinfo.get('child_pid'))",
            "            if not sup_alive and not child_alive and (time.time() - started_at) > 2.0:",
            "                time.sleep(0.2)",
            "                st_obj2 = _read_status()",
            "                if st_obj2 is not None:",
            "                    done = True",
            "                    rc_val = int(st_obj2.get('returncode', 1))",
            "                else:",
            "                    done = True",
            "                    meta_p = os.path.join(cwd, 'result_metadata.json')",
            "                    if check_meta and os.path.isfile(meta_p):",
            "                        try:",
            "                            m_obj = json.loads(open(meta_p, 'r', encoding='utf-8').read())",
            "                            rc_val = 0 if m_obj.get('status') == 'completed' else 2",
            "                        except Exception:",
            "                            rc_val = 1",
            "                    else:",
            "                        rc_val = 1",
            "    except Exception:",
            "        pass",
            "log_tail = ''",
            "if done:",
            "    raw_log = ''",
            "    if os.path.isfile(log_file):",
            "        try:",
            "            with open(log_file, 'r', encoding='utf-8', errors='replace') as lf:",
            "                raw_log = lf.read()",
            "        except Exception:",
            "            raw_log = ''",
            "    structured_lines = [",
            "        ln for ln in raw_log.splitlines()",
            "        if ln.startswith('PIPELINE_RESULT_JSON:') or ln.startswith('SETUP_ENV_RESULT_JSON:')",
            "    ]",
            "    tail_part = raw_log[-24000:] if len(raw_log) > 24000 else raw_log",
            "    for sl in structured_lines:",
            "        if sl not in tail_part:",
            "            tail_part = tail_part + '\\n' + sl",
            "    meta_p = os.path.join(cwd, 'result_metadata.json')",
            "    if check_meta and 'PIPELINE_RESULT_JSON:' not in tail_part and os.path.isfile(meta_p):",
            "        try:",
            "            m_raw = json.loads(open(meta_p, 'r', encoding='utf-8').read())",
            "            tail_part = tail_part + '\\nPIPELINE_RESULT_JSON:' + json.dumps(m_raw, ensure_ascii=False)",
            "        except Exception:",
            "            pass",
            "    if rc_val and int(rc_val) != 0 and 'EXEC_CMD_FAILED_WITH_CODE:' not in tail_part:",
            "        tail_part = tail_part + f'\\nEXEC_CMD_FAILED_WITH_CODE: {int(rc_val)}'",
            "    log_tail = tail_part.strip()",
            "    for fp in (pid_file, status_file):",
            "        try:",
            "            if os.path.exists(fp):",
            "                os.unlink(fp)",
            "        except Exception:",
            "            pass",
            "print(marker + json.dumps({'done': done, 'returncode': rc_val, 'progress': progress, 'log_tail': log_tail}, ensure_ascii=False))",
        ]
    )


def _kill_remote_monitored_job(session_name: str, pid_file: str) -> None:
    """Best-effort terminate a detached remote supervisor & process group upon job cancellation."""
    kill_code = "\n".join(
        [
            "import json, os, signal",
            f"pid_file = {pid_file!r}",
            "if os.path.isfile(pid_file):",
            "    try:",
            "        info = json.loads(open(pid_file, 'r', encoding='utf-8').read())",
            "        for key, use_pg in (('child_pid', True), ('sup_pid', False)):",
            "            pid = info.get(key)",
            "            if isinstance(pid, int) and pid > 1:",
            "                try:",
            "                    if use_pg:",
            "                        os.killpg(pid, signal.SIGTERM)",
            "                    else:",
            "                        os.kill(pid, signal.SIGTERM)",
            "                except Exception:",
            "                    pass",
            "        os.unlink(pid_file)",
            "    except Exception:",
            "        pass",
        ]
    )
    try:
        _run_wsl_colab_argv(
            ["exec", "-s", session_name, "--timeout", "6"],
            timeout=8,
            stdin_text=kill_code,
        )
    except Exception:
        pass


def run_argv_monitored(
    session_name: str,
    argv: Sequence[str],
    *,
    cwd: str = "/content",
    env_vars: Optional[Mapping[str, str]] = None,
    timeout: Optional[int] = None,
    cancel_check: Optional[Callable[[], bool]] = None,
    progress_callback: Optional[Callable[[int, str], None]] = None,
    job_tag: str = "pipeline",
    poll_interval_seconds: float = 5.0,
    max_poll_interval_seconds: Optional[float] = None,
) -> Tuple[bool, str]:
    """
    Launch `argv` detached on the Colab VM and poll short-lived status/progress snapshots.

    Eliminates single-WebSocket `RuntimeError: Connection was lost.` failures across 5–15 min
    GPU runs while streaming live `.pipeline_progress.json` updates back to Dashboard & Telegram.
    """
    safe_session = validate_session_name(session_name)
    safe_argv = _validate_argv(argv)
    if not isinstance(cwd, str) or not cwd or has_control_chars(cwd):
        raise CloudValidationError("Thư mục làm việc cwd trên Colab không hợp lệ.", code="INVALID_INPUT")
    safe_env = _validate_env_vars(env_vars)

    if _is_session_aborted(safe_session) or (cancel_check is not None and cancel_check()):
        return False, "Tác vụ đã bị hủy trước khi thực thi lệnh."

    wait_time = int(timeout or DEFAULT_CONFIG["exec_timeout_seconds"])
    run_id = uuid.uuid4().hex[:10]
    clean_s = re.sub(r"[^A-Za-z0-9_.-]", "", safe_session)
    safe_tag = re.sub(r"[^a-z0-9_]", "", str(job_tag or "pipeline").lower()) or "pipeline"

    pid_file = f"/content/.autodub_{safe_tag}.pid.json"
    status_file = f"/content/.autodub_{safe_tag}.status.json"
    log_file = f"/content/.autodub_{safe_tag}.run.log"
    progress_file = f"{cwd.rstrip('/')}/.pipeline_progress.json"

    local_env_file: Optional[Path] = None
    remote_env_path: Optional[str] = None
    secret_values = list(safe_env.values())

    try:
        if safe_env:
            env_name = f".colab_env_{clean_s}_{run_id}.json"
            fd, raw_tmp = tempfile.mkstemp(prefix=f"colab_env_{clean_s}_", suffix=".json")
            local_env_file = Path(raw_tmp)
            try:
                os.chmod(raw_tmp, stat.S_IRUSR | stat.S_IWUSR)
            except Exception:
                pass
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(safe_env, handle, ensure_ascii=False)
            if cancel_check is not None:
                up_ok, up_msg = upload_file(
                    safe_session,
                    local_env_file,
                    env_name,
                    cancel_check=cancel_check,
                )
            else:
                up_ok, up_msg = upload_file(safe_session, local_env_file, env_name)
            if not up_ok:
                return False, redact_secrets(f"Không thể nạp cấu hình phiên tạm: {up_msg}", extra_secrets=secret_values)
            remote_env_path = f"/content/{env_name}"

        if _is_session_aborted(safe_session) or (cancel_check is not None and cancel_check()):
            return False, "Tác vụ đã bị hủy bởi yêu cầu dừng."

        launcher_py = _build_detached_launcher_script(
            safe_argv,
            cwd=cwd,
            run_id=run_id,
            safe_tag=safe_tag,
            remote_env_path=remote_env_path,
            pid_file=pid_file,
            status_file=status_file,
            log_file=log_file,
            progress_file=progress_file,
        )
        poller_py = _build_detached_poller_script(
            cwd=cwd,
            run_id=run_id,
            safe_tag=safe_tag,
            pid_file=pid_file,
            status_file=status_file,
            log_file=log_file,
            progress_file=progress_file,
        )
        logger.info(
            "Khởi chạy tách nền (%s) trên session %s: %s (args=%d)...",
            safe_tag,
            safe_session,
            safe_argv[0],
            len(safe_argv) - 1,
        )
        launched_ok = False
        launch_combined = ""
        for launch_try in range(1, 4):
            rc_l, out_l, err_l = _run_wsl_colab_argv(
                ["exec", "-s", safe_session, "--timeout", "30"],
                timeout=40,
                stdin_text=launcher_py,
                extra_secrets=secret_values,
                cancel_check=cancel_check,
                session_name=safe_session,
            )
            launch_combined = redact_secrets(f"{out_l}\n{err_l}".strip(), extra_secrets=secret_values)
            if rc_l == -3:
                _kill_remote_monitored_job(safe_session, pid_file)
                return False, "Tác vụ đã bị hủy bởi yêu cầu dừng."
            if rc_l == 0 and _AUTODUB_LAUNCH_MARKER in out_l:
                launched_ok = True
                break
            if launch_try < 3 and _is_transient_transport_error(launch_combined):
                logger.warning(
                    "Khởi chạy %s trên %s gặp rớt kết nối tạm (lần %d), tự thử lại...",
                    safe_tag,
                    safe_session,
                    launch_try,
                )
                if _wait_interruptible(2.0, session_name=safe_session, cancel_check=cancel_check):
                    _kill_remote_monitored_job(safe_session, pid_file)
                    return False, "Tác vụ đã bị hủy bởi yêu cầu dừng."
                continue
            break

        if not launched_ok:
            return False, launch_combined or f"Không thể khởi chạy tiến trình {safe_tag} trên Colab."

        overall_deadline = time.monotonic() + max(5.0, float(wait_time))
        consecutive_poll_failures = 0
        max_consecutive_failures = 15
        last_progress_sig: Optional[Tuple[int, str]] = None
        base_poll_sleep = max(0.5, float(poll_interval_seconds))
        configured_max_poll = (
            DEFAULT_CONFIG.get("cloud_poll_max_interval_seconds", 30)
            if max_poll_interval_seconds is None
            else max_poll_interval_seconds
        )
        max_poll_sleep = max(base_poll_sleep, float(configured_max_poll))
        poll_sleep = base_poll_sleep

        while time.monotonic() < overall_deadline:
            if _wait_interruptible(poll_sleep, session_name=safe_session, cancel_check=cancel_check):
                _kill_remote_monitored_job(safe_session, pid_file)
                return False, "Tác vụ đã bị hủy bởi yêu cầu dừng."

            rc_p, out_p, err_p = _run_wsl_colab_argv(
                ["exec", "-s", safe_session, "--timeout", "20"],
                timeout=25,
                stdin_text=poller_py,
                extra_secrets=secret_values,
                cancel_check=cancel_check,
                session_name=safe_session,
            )
            if rc_p == -3:
                _kill_remote_monitored_job(safe_session, pid_file)
                return False, "Tác vụ đã bị hủy bởi yêu cầu dừng."

            poll_payload: Optional[Dict[str, Any]] = None
            if rc_p == 0 and _AUTODUB_POLL_MARKER in out_p:
                for line in reversed(out_p.splitlines()):
                    if line.startswith(_AUTODUB_POLL_MARKER):
                        try:
                            parsed = json.loads(line[len(_AUTODUB_POLL_MARKER) :])
                            if isinstance(parsed, dict):
                                poll_payload = parsed
                                break
                        except Exception:
                            pass

            if poll_payload is None:
                consecutive_poll_failures += 1
                poll_sleep = base_poll_sleep
                err_snip = redact_secrets(f"{out_p}\n{err_p}".strip(), extra_secrets=secret_values)
                logger.warning(
                    "Poll %s trên %s tạm gián đoạn (lần %d/%d): %s",
                    safe_tag,
                    safe_session,
                    consecutive_poll_failures,
                    max_consecutive_failures,
                    err_snip[:200],
                )
                if consecutive_poll_failures >= max_consecutive_failures:
                    return (
                        False,
                        f"Mất kết nối liên tục tới Colab session {safe_session} ({consecutive_poll_failures} lần thử): {err_snip}",
                    )
                continue

            consecutive_poll_failures = 0
            prog = poll_payload.get("progress")
            progress_sig: Optional[Tuple[int, str]] = None
            if isinstance(prog, dict):
                try:
                    p_pct = int(prog.get("percent") or 0)
                    p_msg = redact_secrets(str(prog.get("message") or "").strip(), extra_secrets=secret_values)
                    if p_pct > 0 and p_msg:
                        progress_sig = (p_pct, p_msg)
                except Exception:
                    pass
            if progress_sig != last_progress_sig:
                last_progress_sig = progress_sig
                poll_sleep = base_poll_sleep
                if progress_sig is not None and progress_callback is not None:
                    try:
                        progress_callback(*progress_sig)
                    except Exception:
                        pass
            else:
                poll_sleep = min(max_poll_sleep, poll_sleep * 1.5)

            if poll_payload.get("done"):
                remote_rc = int(poll_payload.get("returncode") if poll_payload.get("returncode") is not None else 1)
                final_log = redact_secrets(str(poll_payload.get("log_tail") or ""), extra_secrets=secret_values)
                is_ok = (
                    (remote_rc == 0)
                    and ("EXEC_CMD_FAILED_WITH_CODE:" not in final_log)
                )
                return is_ok, final_log

        _kill_remote_monitored_job(safe_session, pid_file)
        return False, f"Timeout sau {wait_time} giây khi theo dõi tiến trình {safe_tag} trên Colab."

    finally:
        if local_env_file is not None:
            try:
                local_env_file.unlink(missing_ok=True)
            except Exception:
                pass


def _parse_legacy_command_to_argv(command: str) -> Tuple[str, List[str]]:
    """Convert simple legacy commands (optionally `cd <dir> && <cmd...>`) safely to (cwd, argv)."""
    if not isinstance(command, str) or not command.strip() or has_control_chars(command):
        raise CloudValidationError("Lệnh thực thi không hợp lệ hoặc chứa ký tự điều khiển.", code="INVALID_INPUT")

    cwd = "/content"
    expr = command.strip()
    if expr.startswith("cd "):
        if " && " not in expr:
            raise CloudValidationError("Cú pháp cd phải đi kèm '&& <lệnh>'.", code="INVALID_INPUT")
        cd_part, expr = expr.split(" && ", 1)
        cd_tokens = shlex.split(cd_part)
        if len(cd_tokens) != 2 or cd_tokens[0] != "cd":
            raise CloudValidationError("Lệnh chuyển thư mục không hợp lệ.", code="INVALID_INPUT")
        cwd = cd_tokens[1]

    for bad_token in _DISALLOWED_SHELL_META:
        if bad_token in expr:
            raise CloudValidationError(
                f"Không cho phép toán tử shell '{bad_token}'; hãy dùng exec_argv.",
                code="INVALID_INPUT",
            )

    tokens = shlex.split(expr)
    if not tokens:
        raise CloudValidationError("Thiếu câu lệnh thực thi.", code="INVALID_INPUT")
    return cwd, tokens


def exec_cmd(
    session_name: str,
    command: str,
    timeout: Optional[int] = None,
    *,
    cancel_check: Optional[Callable[[], bool]] = None,
) -> Tuple[bool, str]:
    """
    Backward-compatible command wrapper parsed safely with `shlex.split` and run via `exec_argv` (`shell=False`).
    """
    cwd, argv = _parse_legacy_command_to_argv(command)
    if cancel_check is not None:
        return exec_argv(session_name, argv, cwd=cwd, timeout=timeout, cancel_check=cancel_check)
    return exec_argv(session_name, argv, cwd=cwd, timeout=timeout)


def download_file(
    session_name: str,
    remote_filename: str,
    local_dest_dir: str | Path,
    *,
    cancel_check: Optional[Callable[[], bool]] = None,
) -> Tuple[bool, str]:
    """Download a produced file from `/content/...` on Colab to a local destination directory."""
    safe_session = validate_session_name(session_name)
    if _is_session_aborted(safe_session) or (cancel_check is not None and cancel_check()):
        return False, "Tác vụ đã bị hủy trước khi download."

    if not remote_filename or has_control_chars(remote_filename) or ".." in Path(remote_filename).parts:
        raise CloudValidationError("Đường dẫn file tải về từ Colab không hợp lệ.", code="INVALID_INPUT")

    dest_dir = Path(local_dest_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)
    expected_file = dest_dir / Path(remote_filename).name
    wsl_dest_file = to_wsl_path(expected_file)
    remote_src = remote_filename if remote_filename.startswith("content/") else f"content/{remote_filename}"
    timeout = int(DEFAULT_CONFIG["download_timeout_seconds"])

    logger.info("Đang download %s từ session %s về %s...", remote_src, safe_session, expected_file)
    combined = ""

    def remove_partial() -> Optional[str]:
        try:
            if expected_file.exists() or expected_file.is_symlink():
                expected_file.unlink()
        except OSError as exc:
            return redact_secrets(str(exc))
        return None

    for attempt_idx in range(1, 3):
        cleanup_error = remove_partial()
        if cleanup_error:
            return False, f"Không thể xóa file download cũ: {cleanup_error}"
        rc, stdout, stderr = _run_wsl_colab_argv(
            ["download", "-s", safe_session, remote_src, wsl_dest_file],
            timeout=timeout,
            cancel_check=cancel_check,
            session_name=safe_session,
        )
        combined = redact_secrets(f"{stdout}\n{stderr}".strip())
        if rc == -3:
            remove_partial()
            return False, f"Tác vụ đã bị hủy bởi yêu cầu dừng. {combined}".strip() if combined else "Tác vụ đã bị hủy bởi yêu cầu dừng."
        file_ready = False
        try:
            file_ready = expected_file.is_file() and expected_file.stat().st_size > 0
        except OSError:
            pass
        if rc == 0 and not _TRANSFER_FAILURE_RE.search(combined) and file_ready:
            logger.info("Download %s thành công.", Path(remote_filename).name)
            return True, str(expected_file)
        cleanup_error = remove_partial()
        if cleanup_error:
            combined = f"{combined}\nKhông thể xóa file download không hợp lệ: {cleanup_error}".strip()
        if attempt_idx < 2 and _is_transient_transport_error(combined):
            logger.warning("Download %s gặp rớt kết nối tạm (lần %d), tự thử lại...", Path(remote_filename).name, attempt_idx)
            if _wait_interruptible(1.5, session_name=safe_session, cancel_check=cancel_check):
                return False, "Tác vụ đã bị hủy bởi yêu cầu dừng."
            continue
        break

    return False, combined or "Lỗi download."


def stop_session(session_name: str) -> Tuple[bool, str]:
    """Stop and remove strictly the specified Colab session cleanly (no account-wide wipe)."""
    safe_session = validate_session_name(session_name)
    abort_session_processes(safe_session)
    timeout = int(DEFAULT_CONFIG["stop_timeout_seconds"])
    logger.info("Đang dừng session Colab: %s...", safe_session)
    rc, stdout, stderr = _run_wsl_colab_argv(["stop", "-s", safe_session], timeout=timeout)
    combined = redact_secrets(f"{stdout}\n{stderr}".strip())
    if rc == 0:
        logger.info("Đã dừng session %s an toàn.", safe_session)
        return True, "Session đã dừng."

    return False, combined or "Không thể dừng session."


def verify_session_stopped(
    session_name: str,
    *,
    max_retries: int = 3,
    retry_delay: float = 0.2,
) -> Dict[str, Any]:
    """Stop `session_name` with bounded retries and verify via `get_sessions_status()`."""
    safe_session = validate_session_name(session_name)
    retries = max(1, int(max_retries))
    last_error: Optional[str] = None
    saw_unreachable = False

    for attempt_idx in range(1, retries + 1):
        stop_ok, stop_msg = stop_session(safe_session)
        status = get_sessions_status()
        if status.get("ok"):
            sessions = status.get("sessions") or []
            still_present = any(str(item.get("name")) == safe_session for item in sessions)
            if not still_present:
                return {
                    "verified_stopped": True,
                    "cleanup_status": "verified",
                    "attempts": attempt_idx,
                    "error": None,
                }
            if still_present:
                last_error = f"Session {safe_session} vẫn còn trong danh sách hoạt động."
            else:
                last_error = stop_msg or f"Không thể xác nhận dừng session {safe_session}."
        else:
            saw_unreachable = True
            last_error = status.get("error") or stop_msg or "Không kết nối được provider Colab."

        if attempt_idx < retries and retry_delay > 0:
            time.sleep(retry_delay)

    return {
        "verified_stopped": False,
        "cleanup_status": "provider_unreachable" if saw_unreachable else "failed_retry_needed",
        "attempts": retries,
        "error": redact_secrets(last_error) if last_error else "Không thể xác nhận dừng session.",
    }


def stop_all_sessions() -> Tuple[bool, str]:
    """Acknowledge shutdown only after a successful remote empty-inventory check."""
    logger.info("Đang quét và thu hồi toàn bộ máy ảo Colab...")
    marker = "AUTODUB_STOP_VERIFIED:"
    py_cleanup = "\n".join([
        "import json, sys",
        "sys.path.insert(0, '/root/.local/share/uv/tools/google-colab-cli/lib/python3.14/site-packages')",
        "from colab_cli.common import state",
        "errors = []",
        "for assignment in list(state.client.list_assignments()):",
        "    try:",
        "        state.client.unassign(assignment.endpoint)",
        "    except Exception as exc:",
        "        errors.append(str(exc))",
        "remaining = list(state.client.list_assignments())",
        "if remaining or errors:",
        "    raise RuntimeError('Shutdown unverified: remaining=%d; errors=%s' % (len(remaining), errors))",
        "for name in list(state.store.list().keys()):",
        "    state.store.remove(name)",
        f"print({marker!r} + json.dumps({{'remaining': 0}}))",
    ])
    try:
        rc, stdout, stderr = _run_wsl_argv(
            "/usr/bin/python3", ["-"], stdin_text=py_cleanup,
            timeout=int(DEFAULT_CONFIG["stop_timeout_seconds"]),
        )
        verified = any(
            line.startswith(marker) and json.loads(line[len(marker):]) == {"remaining": 0}
            for line in stdout.splitlines()
        )
        if rc != 0 or not verified:
            detail = redact_secrets(stderr or stdout or f"exit={rc}")[-500:]
            return False, "Chưa xác nhận thu hồi Cloud: " + detail
    except Exception as exc:
        return False, "Chưa xác nhận thu hồi Cloud: " + redact_secrets(str(exc))
    return True, "Đã thu hồi và dừng toàn bộ máy ảo Colab an toàn."


class ColabProviderAdapter:
    """Provider adapter separating orchestrator lifecycle logic from Colab CLI transport."""

    def get_usage(self) -> Dict[str, Any]:
        return get_usage()

    def get_sessions(self) -> List[Dict[str, Any]]:
        return get_sessions()

    def get_sessions_status(self) -> Dict[str, Any]:
        return get_sessions_status()

    def create_session(
        self,
        session_name: str,
        gpu: str = "T4",
        *,
        cancel_check: Optional[Callable[[], bool]] = None,
    ) -> Tuple[bool, str]:
        return create_session(session_name, gpu=gpu, cancel_check=cancel_check)

    def mount_drive(
        self,
        session_name: str,
        mount_path: str = "/content/drive",
        *,
        timeout: int = 180,
        cancel_check: Optional[Callable[[], bool]] = None,
    ) -> Tuple[bool, str]:
        return mount_drive(
            session_name,
            mount_path,
            timeout=timeout,
            cancel_check=cancel_check,
        )

    def upload_file(
        self,
        session_name: str,
        local_path: str | Path,
        remote_filename: Optional[str] = None,
        *,
        cancel_check: Optional[Callable[[], bool]] = None,
    ) -> Tuple[bool, str]:
        return upload_file(session_name, local_path, remote_filename=remote_filename, cancel_check=cancel_check)

    def exec_argv(
        self,
        session_name: str,
        argv: Sequence[str],
        *,
        cwd: str = "/content",
        env_vars: Optional[Mapping[str, str]] = None,
        timeout: Optional[int] = None,
        cancel_check: Optional[Callable[[], bool]] = None,
    ) -> Tuple[bool, str]:
        return exec_argv(
            session_name,
            argv,
            cwd=cwd,
            env_vars=env_vars,
            timeout=timeout,
            cancel_check=cancel_check,
        )

    def run_argv_monitored(
        self,
        session_name: str,
        argv: Sequence[str],
        *,
        cwd: str = "/content",
        env_vars: Optional[Mapping[str, str]] = None,
        timeout: Optional[int] = None,
        cancel_check: Optional[Callable[[], bool]] = None,
        progress_callback: Optional[Callable[[int, str], None]] = None,
        job_tag: str = "pipeline",
        poll_interval_seconds: float = 5.0,
        max_poll_interval_seconds: Optional[float] = None,
    ) -> Tuple[bool, str]:
        return run_argv_monitored(
            session_name,
            argv,
            cwd=cwd,
            env_vars=env_vars,
            timeout=timeout,
            cancel_check=cancel_check,
            progress_callback=progress_callback,
            job_tag=job_tag,
            poll_interval_seconds=poll_interval_seconds,
            max_poll_interval_seconds=max_poll_interval_seconds,
        )

    def exec_script(
        self,
        session_name: str,
        script_local_path: str | Path,
        timeout: Optional[int] = None,
        *,
        cancel_check: Optional[Callable[[], bool]] = None,
    ) -> Tuple[bool, str]:
        return exec_script(session_name, script_local_path, timeout=timeout, cancel_check=cancel_check)

    def download_file(
        self,
        session_name: str,
        remote_filename: str,
        local_dest_dir: str | Path,
        *,
        cancel_check: Optional[Callable[[], bool]] = None,
    ) -> Tuple[bool, str]:
        return download_file(session_name, remote_filename, local_dest_dir, cancel_check=cancel_check)

    def stop_session(self, session_name: str) -> Tuple[bool, str]:
        return stop_session(session_name)

    def verify_session_stopped(
        self,
        session_name: str,
        *,
        max_retries: int = 3,
        retry_delay: float = 0.2,
    ) -> Dict[str, Any]:
        return verify_session_stopped(session_name, max_retries=max_retries, retry_delay=retry_delay)

    def abort_session_processes(self, session_name: str) -> int:
        return abort_session_processes(session_name)

    def stop_all_sessions(self) -> Tuple[bool, str]:
        return stop_all_sessions()
