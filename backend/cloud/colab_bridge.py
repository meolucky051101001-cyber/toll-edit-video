"""
colab_bridge.py - Wrapper điều khiển Colab CLI qua WSL2 cho Tool V1.
Thực hiện các thao tác quản lý session, upload, exec, download và stop an toàn.
"""

import logging
import json
import re
import shlex
import subprocess
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .cloud_config import DEFAULT_CONFIG, get_wsl_env_prefix, to_wsl_path

logger = logging.getLogger("cloud.colab_bridge")


def _run_wsl_bash(
    bash_cmd: str,
    timeout: int = 300,
    distro: Optional[str] = None,
    user: Optional[str] = None,
) -> Tuple[int, str, str]:
    """
    Thực thi lệnh bash trực tiếp bên trong WSL2.
    """
    target_distro = distro or DEFAULT_CONFIG["wsl_distro"]
    target_user = user or DEFAULT_CONFIG["wsl_user"]
    prefix = get_wsl_env_prefix()
    full_cmd = f"{prefix}{bash_cmd}"

    cmd_args = [
        "wsl",
        "-d",
        target_distro,
        "-u",
        target_user,
        "--",
        "bash",
        "-c",
        full_cmd,
    ]

    try:
        proc = subprocess.run(
            cmd_args,
            capture_output=True,
            timeout=timeout,
        )
        stdout = proc.stdout.decode("utf-8", errors="replace").strip()
        stderr = proc.stderr.decode("utf-8", errors="replace").strip()

        if proc.returncode != 0:
            logger.warning(
                "WSL bash lỗi (rc=%d): %s | stderr: %s",
                proc.returncode,
                bash_cmd.split()[0] if bash_cmd else "",
                stderr[:300],
            )
        return proc.returncode, stdout, stderr

    except subprocess.TimeoutExpired:
        logger.error("Lệnh wsl timeout (%ds): %s", timeout, bash_cmd.split()[0] if bash_cmd else "")
        return -1, "", f"Timeout sau {timeout} giây."
    except Exception as exc:
        logger.error("Lỗi ngoại lệ khi gọi WSL: %s", exc)
        return -2, "", str(exc)


def _run_wsl_colab(
    colab_subcommand: str,
    timeout: int = 300,
    distro: Optional[str] = None,
    user: Optional[str] = None,
) -> Tuple[int, str, str]:
    """
    Thực thi lệnh Colab CLI bên trong WSL2.
    Đảm bảo luôn export PATH và PYTHON_KEYRING_BACKEND, kèm cờ --auth oauth2.
    """
    return _run_wsl_bash(
        f"colab --auth oauth2 {colab_subcommand}",
        timeout=timeout,
        distro=distro,
        user=user,
    )


def get_usage() -> Dict[str, Any]:
    """
    Tra cứu số dư Compute Units (CU), tỷ lệ tiêu thụ và số máy đang chạy.
    Trả về dict: {"balance": float, "usage_rate": float, "active_assignments": int, "ok": bool}
    """
    rc, stdout, stderr = _run_wsl_colab("usage", timeout=30)
    data = {
        "balance": 0.0,
        "usage_rate": 0.0,
        "active_assignments": 0,
        "raw": stdout or stderr,
        "ok": rc == 0,
    }
    if rc != 0:
        return data

    # Parse output ví dụ:
    # Current balance: 200.00 compute units
    # Usage rate: 0.00/hr
    # Active assignments: 0
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


def get_sessions() -> List[Dict[str, Any]]:
    """
    Lấy danh sách các session đang mở trên Colab.
    """
    rc, stdout, _ = _run_wsl_colab("sessions", timeout=30)
    if rc != 0 or not stdout:
        return []

    if "No active sessions found" in stdout or "No active sessions" in stdout:
        return []

    sessions = []
    lines = stdout.splitlines()
    for line in lines:
        line_clean = line.strip()
        if not line_clean or line_clean.startswith("[colab]") or "Active sessions" in line_clean:
            continue
        # Format dòng ví dụ: [*] session-name | Hardware: T4 | Shape: Standard
        left_part = line_clean.split("|")[0].strip()
        tokens = left_part.split()
        if not tokens:
            continue
        s_name = tokens[-1]
        is_active = not ("[?]" in tokens or "[-]" in tokens)
        sessions.append({
            "name": s_name,
            "raw": line_clean,
            "is_active": is_active,
        })
    return sessions


def create_session(session_name: str, gpu: str = "T4") -> Tuple[bool, str]:
    """
    Tạo session Colab mới với loại GPU, TPU hoặc CPU chỉ định.
    Hỗ trợ đầy đủ: T4, L4, A100, H100, G4, TPU (V5E1, V6E1), CPU.
    """
    raw_acc = (gpu or "T4").strip().upper()

    if "TPU" in raw_acc or raw_acc in ("V5E1", "V6E1"):
        tpu_type = "v6e1" if "6" in raw_acc else "v5e1"
        sub_flag = f"--tpu {tpu_type}"
        target_desc = f"TPU {tpu_type.upper()}"
    elif raw_acc == "CPU":
        sub_flag = ""
        target_desc = "CPU"
    else:
        # GPU các dòng
        valid_gpus = {"T4": "T4", "L4": "L4", "A100": "A100", "H100": "H100", "G4": "G4"}
        gpu_type = valid_gpus.get(raw_acc, "T4")
        sub_flag = f"--gpu {gpu_type}"
        target_desc = f"GPU {gpu_type}"

    logger.info("Đang tạo session Colab: %s (%s)...", session_name, target_desc)
    cmd_str = f"new -s {shlex.quote(session_name)} {sub_flag}".strip()
    rc, stdout, stderr = _run_wsl_colab(cmd_str, timeout=120)
    combined = f"{stdout}\n{stderr}".strip()
    if rc == 0:
        logger.info("Tạo session %s thành công.", session_name)
        return True, combined or "Tạo session thành công."

    # Phát hiện lỗi 503 hết máy
    if "503" in combined or "unavailable" in combined.lower() or "no machines" in combined.lower():
        msg = f"Hết máy {target_desc} trên Colab (503 Server Error). Hãy thử lại sau hoặc chuyển sang GPU khác."
        logger.warning(msg)
        return False, msg

    return False, combined or "Không thể tạo session."


def upload_file(session_name: str, local_path: str | Path, remote_filename: Optional[str] = None) -> Tuple[bool, str]:
    """
    Upload một file từ máy Windows lên session Colab.
    Cú pháp Colab CLI: colab upload -s <session> <local_path> <remote_path>
    """
    path_obj = Path(local_path)
    if not path_obj.exists():
        return False, f"File không tồn tại trên máy: {local_path}"

    wsl_file = to_wsl_path(path_obj)
    remote_base = remote_filename or path_obj.name
    remote_dest = remote_base if remote_base.startswith("content/") else f"content/{remote_base}"
    timeout = DEFAULT_CONFIG["upload_timeout_seconds"]

    logger.info("Đang upload %s lên session %s (remote: %s)...", path_obj.name, session_name, remote_dest)
    rc, stdout, stderr = _run_wsl_colab(
        f"upload -s {shlex.quote(session_name)} {shlex.quote(wsl_file)} {shlex.quote(remote_dest)}",
        timeout=timeout,
    )
    combined = f"{stdout}\n{stderr}".strip()
    if rc == 0:
        logger.info("Upload %s thành công.", path_obj.name)
        return True, "Upload thành công."

    return False, combined or "Lỗi upload."


def exec_script(
    session_name: str,
    script_local_path: str | Path,
    timeout: Optional[int] = None,
) -> Tuple[bool, str]:
    """
    Thực thi file script Python trên session Colab thông qua cờ -f.
    """
    path_obj = Path(script_local_path)
    if not path_obj.exists():
        return False, f"File script không tồn tại: {script_local_path}"

    wsl_script = to_wsl_path(path_obj)
    wait_time = timeout or DEFAULT_CONFIG["exec_timeout_seconds"]

    logger.info("Đang chạy script %s trên session %s...", path_obj.name, session_name)
    rc, stdout, stderr = _run_wsl_colab(
        f"exec -s {shlex.quote(session_name)} -f {shlex.quote(wsl_script)}",
        timeout=wait_time,
    )
    combined = f"{stdout}\n{stderr}".strip()
    is_ok = (rc == 0) and ("EXEC_CMD_FAILED_WITH_CODE:" not in combined) and ("SyntaxError" not in combined)
    return is_ok, combined


def exec_cmd(
    session_name: str,
    command: str,
    timeout: Optional[int] = None,
) -> Tuple[bool, str]:
    """
    Thực thi một câu lệnh bash trên session Colab thông qua remote Jupyter kernel.
    """
    wait_time = timeout or DEFAULT_CONFIG["exec_timeout_seconds"]
    clean_s = re.sub(r"[^a-zA-Z0-9]", "", session_name)
    wsl_tmp_script = f"/tmp/colab_cmd_{clean_s}.py"

    py_wrapper = (
        "import subprocess, sys\n"
        f"cmd = {command!r}\n"
        "p = subprocess.Popen(cmd, shell=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)\n"
        "for line in p.stdout:\n"
        "    sys.stdout.write(line)\n"
        "    sys.stdout.flush()\n"
        "p.wait()\n"
        "if p.returncode != 0:\n"
        "    print('EXEC_CMD_FAILED_WITH_CODE:', p.returncode)\n"
        "    sys.exit(p.returncode)\n"
    )

    # Ghi file tạm vào WSL
    cmd_write = [
        "wsl",
        "-d",
        DEFAULT_CONFIG["wsl_distro"],
        "-u",
        DEFAULT_CONFIG["wsl_user"],
        "--",
        "python3",
        "-c",
        f"with open('{wsl_tmp_script}', 'w', encoding='utf-8') as f: f.write({py_wrapper!r})",
    ]
    subprocess.run(cmd_write, capture_output=True, timeout=15)

    logger.info("Đang thực thi lệnh trên session %s", session_name)
    rc, stdout, stderr = _run_wsl_colab(
        f"exec -s {shlex.quote(session_name)} -f {wsl_tmp_script} --timeout 0",
        timeout=wait_time,
    )

    # Dọn dẹp file tạm
    cmd_rm = [
        "wsl",
        "-d",
        DEFAULT_CONFIG["wsl_distro"],
        "-u",
        DEFAULT_CONFIG["wsl_user"],
        "--",
        "rm",
        "-f",
        wsl_tmp_script,
    ]
    subprocess.run(cmd_rm, capture_output=True, timeout=10)

    combined = f"{stdout}\n{stderr}".strip()
    is_ok = (rc == 0) and ("EXEC_CMD_FAILED_WITH_CODE:" not in combined) and ("SyntaxError" not in combined)
    return is_ok, combined


def download_file(
    session_name: str,
    remote_filename: str,
    local_dest_dir: str | Path,
) -> Tuple[bool, str]:
    """
    Tải file thành phẩm từ session Colab về thư mục máy local.
    Cú pháp Colab CLI: colab download -s <session> <remote_path> <local_path>
    """
    dest_dir = Path(local_dest_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)
    expected_file = dest_dir / Path(remote_filename).name
    wsl_dest_file = to_wsl_path(expected_file)
    remote_src = remote_filename if remote_filename.startswith("content/") else f"content/{remote_filename}"
    timeout = DEFAULT_CONFIG["download_timeout_seconds"]

    logger.info("Đang download %s từ session %s về %s...", remote_src, session_name, expected_file)
    rc, stdout, stderr = _run_wsl_colab(
        f"download -s {shlex.quote(session_name)} {shlex.quote(remote_src)} {shlex.quote(wsl_dest_file)}",
        timeout=timeout,
    )
    combined = f"{stdout}\n{stderr}".strip()

    if rc == 0 or expected_file.exists():
        logger.info("Download %s thành công.", remote_filename)
        return True, str(expected_file)

    return False, combined or "Lỗi download."


def stop_session(session_name: str) -> Tuple[bool, str]:
    """
    Dừng và huỷ session Colab để tiết kiệm Compute Units.
    LUÔN gọi hàm này khi job kết thúc hoặc gặp ngoại lệ.
    """
    timeout = DEFAULT_CONFIG["stop_timeout_seconds"]
    logger.info("Đang dừng session Colab: %s...", session_name)
    rc, stdout, stderr = _run_wsl_colab(
        f"stop -s {shlex.quote(session_name)}",
        timeout=timeout,
    )
    combined = f"{stdout}\n{stderr}".strip()
    if rc == 0:
        logger.info("Đã dừng session %s an toàn.", session_name)
        return True, "Session đã dừng."
    return False, combined or "Không thể dừng session."


def stop_all_sessions() -> Tuple[bool, str]:
    """Only acknowledge shutdown after a successful remote empty-inventory check."""
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
        rc, stdout, stderr = _run_wsl_bash(
            f"/usr/bin/python3 -c {shlex.quote(py_cleanup)}",
            timeout=DEFAULT_CONFIG["stop_timeout_seconds"],
        )
        verified = any(
            line.startswith(marker) and json.loads(line[len(marker):]) == {"remaining": 0}
            for line in stdout.splitlines()
        )
        if rc != 0 or not verified:
            return False, "Chưa xác nhận thu hồi Cloud: " + (stderr or stdout or f"exit={rc}")[-500:]
    except Exception as exc:
        return False, f"Chưa xác nhận thu hồi Cloud: {exc}"
    return True, "Đã thu hồi và dừng toàn bộ máy ảo Colab an toàn."
