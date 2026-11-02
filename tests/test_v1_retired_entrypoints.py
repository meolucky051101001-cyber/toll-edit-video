"""Tests for retired one-off queue and rollback entrypoints (C2 / F02, F03)."""
from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]

RETIRED_PY_MODULES = (
    ("backend.enqueue_missing_batch", PROJECT_ROOT / "backend" / "enqueue_missing_batch.py"),
    ("backend.sync_capcut_queue", PROJECT_ROOT / "backend" / "sync_capcut_queue.py"),
)

RETIRED_LAUNCHERS = (
    PROJECT_ROOT / "scripts" / "windows" / "RESTORE_SAFE_BASELINE.bat",
    PROJECT_ROOT / "scripts" / "windows" / "ROLLBACK_TRANSLATION_FIX.bat",
    PROJECT_ROOT / "scripts" / "windows" / "RESTORE_VOICE_LOCK_BACKUP.bat",
    PROJECT_ROOT / "rollback_to_safe_state.ps1",
    PROJECT_ROOT / "fixes_for_v1" / "apply_fix_to_v1.bat",
    PROJECT_ROOT / "fixes_for_v1" / "rollback_tool_v1.bat",
    PROJECT_ROOT / "scripts" / "setup_telegram_bot_v2.ps1",
)

FORBIDDEN_DESTRUCTIVE_PATTERNS = (
    "git reset --hard",
    "git clean -fd",
    "git checkout 8c7b99a",
    "git checkout backup/",
    "git checkout -- .",
    "Copy-Item",
    "Remove-Item",
    "Set-EnvValue",
    "Invoke-RestMethod",
    "xhslink.com",
    "v.douyin.com",
    "8393150305",
)


@pytest.mark.parametrize("mod_name,mod_path", RETIRED_PY_MODULES)
def test_importing_retired_queue_modules_in_isolated_subprocess_has_zero_side_effects(
    tmp_path: Path, mod_name: str, mod_path: Path
):
    ws = tmp_path / "workspace"
    bot_sys = ws / "bot_system"
    bot_sys.mkdir(parents=True)
    sentinel_queue = bot_sys / "telegram_queue.json"
    sentinel_queue.write_text('{"items": ["sentinel"], "pid": 999}', encoding="utf-8")

    # Subprocess script that blocks file writes, socket connects, and process spawns during import
    probe_code = f"""
import builtins, importlib, pathlib, socket, subprocess, sys
sys.path.insert(0, {str(PROJECT_ROOT)!r})
sys.path.insert(0, {str(PROJECT_ROOT / 'backend')!r})

orig_open = builtins.open
def guarded_open(file, mode='r', *args, **kwargs):
    if any(m in str(mode) for m in ('w', 'a', 'x', '+')):
        raise RuntimeError(f'Forbidden file write during import: {{file}}')
    return orig_open(file, mode, *args, **kwargs)

def forbidden(*args, **kwargs):
    raise RuntimeError('Forbidden side effect during import')

builtins.open = guarded_open
pathlib.Path.write_text = forbidden
pathlib.Path.write_bytes = forbidden
socket.socket.connect = forbidden
subprocess.Popen = forbidden

mod = importlib.import_module({mod_name!r})
assert hasattr(mod, 'RETIRED_REASON')
assert 'RETIRED' in mod.RETIRED_REASON
"""
    env = dict(os.environ, AUTODUB_WORKSPACE=str(ws), PYTHONDONTWRITEBYTECODE="1")
    res = subprocess.run(
        [sys.executable, "-B", "-c", probe_code],
        cwd=str(tmp_path),
        env=env,
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert res.returncode == 0, res.stderr
    assert sentinel_queue.read_text(encoding="utf-8") == '{"items": ["sentinel"], "pid": 999}'
    assert not (bot_sys / "telegram_queue_trigger.flag").exists()
    assert not (bot_sys / "telegram_queue_reorder.flag").exists()


@pytest.mark.parametrize("_mod_name,mod_path", RETIRED_PY_MODULES)
def test_executing_retired_queue_scripts_returns_nonzero_and_preserves_state(
    tmp_path: Path, _mod_name: str, mod_path: Path
):
    content = mod_path.read_text(encoding="utf-8")
    for pattern in FORBIDDEN_DESTRUCTIVE_PATTERNS:
        assert pattern not in content

    res = subprocess.run(
        [sys.executable, "-B", str(mod_path)],
        cwd=str(tmp_path),
        env=dict(os.environ, AUTODUB_WORKSPACE=str(tmp_path), PYTHONDONTWRITEBYTECODE="1"),
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert res.returncode != 0
    assert "RETIRED" in (res.stderr + res.stdout)


@pytest.mark.parametrize("launcher_path", RETIRED_LAUNCHERS)
def test_retired_bat_and_ps1_launchers_contain_no_destructive_commands_and_exit_nonzero_in_temp(
    tmp_path: Path, launcher_path: Path
):
    assert launcher_path.is_file()
    content = launcher_path.read_text(encoding="utf-8")
    for pattern in FORBIDDEN_DESTRUCTIVE_PATTERNS:
        assert pattern not in content, f"{launcher_path.name} still contains {pattern!r}"

    # Copy stub launcher into isolated temp directory with sentinel files and verify non-zero exit + zero mutation
    temp_repo = tmp_path / "repo"
    (temp_repo / "backend").mkdir(parents=True)
    (temp_repo / "fixes_for_v1").mkdir(parents=True)
    (temp_repo / "scripts").mkdir(parents=True)

    env_sentinel = temp_repo / "backend" / ".env"
    env_sentinel.write_text("BOT_TOKEN=sentinel_v1_token\nGEMINI_MODEL=gemini-2.5-flash\n", encoding="utf-8")
    source_sentinel = temp_repo / "backend" / "sentinel.py"
    source_sentinel.write_text("# sentinel\n", encoding="utf-8")

    rel = launcher_path.relative_to(PROJECT_ROOT)
    temp_launcher = temp_repo / rel
    temp_launcher.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(launcher_path, temp_launcher)

    if temp_launcher.suffix.lower() == ".ps1":
        cmd = [
            "powershell.exe",
            "-NoLogo",
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(temp_launcher),
        ]
    else:
        cmd = ["cmd.exe", "/d", "/c", str(temp_launcher)]

    res = subprocess.run(
        cmd,
        cwd=str(temp_repo),
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert res.returncode != 0
    combined = (res.stdout or "") + (res.stderr or "")
    assert "RETIRED" in combined
    assert env_sentinel.read_text(encoding="utf-8") == "BOT_TOKEN=sentinel_v1_token\nGEMINI_MODEL=gemini-2.5-flash\n"
    assert source_sentinel.read_text(encoding="utf-8") == "# sentinel\n"
