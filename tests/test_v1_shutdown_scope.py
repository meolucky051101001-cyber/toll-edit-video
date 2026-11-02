"""Tests for scoped, verified service and worker shutdown (C1 / F01)."""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from backend.process_killer import terminate_worker_processes
from backend.v1_shutdown import (
    EXIT_ACCESS_DENIED,
    EXIT_AMBIGUOUS,
    EXIT_BUSY,
    EXIT_OK,
    EXIT_SURVIVOR,
    ProcessRecord,
    SelectedTarget,
    default_process_terminator,
    execute_shutdown,
    select_shutdown_targets,
    split_windows_command_line,
    verify_telemetry_pid,
)
from backend.v1_stop_bot import stop_v1_bot


@pytest.fixture(autouse=True)
def isolated_task_inventory(monkeypatch):
    # Legacy scope tests model processes, never the machine's real Task Scheduler.
    monkeypatch.setattr("backend.v1_shutdown.enumerate_scheduled_tasks", lambda: [])


def _fixture_processes(project_root: Path) -> list[ProcessRecord]:
    root_str = str(project_root)
    backend_str = str(project_root / "backend")
    venv_py = str(project_root / "backend" / "venv" / "Scripts" / "python.exe")
    venv_pyw = str(project_root / "backend" / "venv" / "Scripts" / "pythonw.exe")
    base_pyw = r"C:\Users\admin\AppData\Local\Programs\Python\Python312\pythonw.exe"
    base_py = r"C:\Users\admin\AppData\Local\Programs\Python\Python312\python.exe"

    return [
        # 1. Legitimate Tool V1 supervisor (venv wrapper + base python child + worker)
        ProcessRecord(
            pid=1001,
            ppid=2404,
            name="pythonw.exe",
            exe=venv_pyw,
            cmdline=(venv_pyw, f"{backend_str}\\background_service.py", "--service", "telegram"),
            cwd=backend_str,
            create_time=1000.0,
        ),
        ProcessRecord(
            pid=1002,
            ppid=1001,
            name="pythonw.exe",
            exe=base_pyw,
            cmdline=(venv_pyw, f"{backend_str}\\background_service.py", "--service", "telegram"),
            cwd=backend_str,
            create_time=1000.1,
        ),
        ProcessRecord(
            pid=1003,
            ppid=1002,
            name="python.exe",
            exe=venv_py,
            cmdline=(venv_py, "-u", f"{backend_str}\\telegram_bot.py"),
            cwd=backend_str,
            create_time=1001.0,
        ),
        ProcessRecord(
            pid=1004,
            ppid=1003,
            name="python.exe",
            exe=base_py,
            cmdline=(venv_py, "-u", f"{backend_str}\\telegram_bot.py"),
            cwd=backend_str,
            create_time=1001.1,
        ),
        # 2. Legitimate Tool V1 dashboard & control & watchdog
        ProcessRecord(
            pid=1010,
            ppid=2404,
            name="python.exe",
            exe=venv_py,
            cmdline=(venv_py, f"{backend_str}\\main.py"),
            cwd=backend_str,
            create_time=1002.0,
        ),
        ProcessRecord(
            pid=1011,
            ppid=2404,
            name="pythonw.exe",
            exe=venv_pyw,
            cmdline=(venv_pyw, f"{backend_str}\\tool_control.py"),
            cwd=backend_str,
            create_time=1003.0,
        ),
        ProcessRecord(
            pid=1012,
            ppid=2404,
            name="pythonw.exe",
            exe=venv_pyw,
            cmdline=(venv_pyw, f"{backend_str}\\ops_watchdog.py", "--heal"),
            cwd=backend_str,
            create_time=1004.0,
        ),
        # 3. Standalone downloader bot (only included in downloader/cutover or explicit flag)
        ProcessRecord(
            pid=1020,
            ppid=2404,
            name="pythonw.exe",
            exe=venv_pyw,
            cmdline=(venv_pyw, f"{root_str}\\telebot_video_downloader.py"),
            cwd=root_str,
            create_time=1005.0,
        ),
        # 4. Verified Tool V1 media worker and OCR worker
        ProcessRecord(
            pid=1030,
            ppid=1010,
            name="ffmpeg.exe",
            exe=r"C:\ffmpeg\bin\ffmpeg.exe",
            cmdline=("ffmpeg.exe", "-i", f"{root_str}\\workspace\\job1\\input.mp4", "-y", f"{root_str}\\workspace\\job1\\out.wav"),
            cwd=backend_str,
            create_time=1010.0,
        ),
        ProcessRecord(
            pid=1031,
            ppid=1,
            name="python.exe",
            exe=venv_py,
            cmdline=(venv_py, "-m", "v1_ocr_worker"),
            cwd=backend_str,
            create_time=1011.0,
        ),
        # 5. NON-TARGETS that old substring match (`*tool v1*` or `temp` or `workspace`) wrongly killed:
        # 5a. Sibling directory C:\tool v10
        ProcessRecord(
            pid=9001,
            ppid=2404,
            name="python.exe",
            exe=r"C:\tool v10\backend\venv\Scripts\python.exe",
            cmdline=(r"C:\tool v10\backend\venv\Scripts\python.exe", r"C:\tool v10\backend\main.py"),
            cwd=r"C:\tool v10\backend",
            create_time=900.0,
        ),
        # 5b. PowerShell window with project path in command line
        ProcessRecord(
            pid=9002,
            ppid=2404,
            name="powershell.exe",
            exe=r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe",
            cmdline=("powershell.exe", "-Command", f"Set-Location '{root_str}'; Get-Status"),
            cwd=root_str,
            create_time=901.0,
        ),
        # 5c. Codex / Editor / IDE python helper mentioning project root
        ProcessRecord(
            pid=9003,
            ppid=2404,
            name="python.exe",
            exe=base_py,
            cmdline=(base_py, r"C:\Users\admin\.vscode\extensions\lsp_server.py", "--workspace", root_str),
            cwd=r"C:\Users\admin",
            create_time=902.0,
        ),
        # 5d. Unrelated Python app
        ProcessRecord(
            pid=9004,
            ppid=2404,
            name="python.exe",
            exe=r"C:\other_app\venv\Scripts\python.exe",
            cmdline=(r"C:\other_app\venv\Scripts\python.exe", r"C:\other_app\main.py"),
            cwd=r"C:\other_app",
            create_time=903.0,
        ),
        # 5e. Unrelated ffmpeg with 'temp' and 'workspace' in path
        ProcessRecord(
            pid=9005,
            ppid=2404,
            name="ffmpeg.exe",
            exe=r"C:\ffmpeg\bin\ffmpeg.exe",
            cmdline=("ffmpeg.exe", "-i", r"C:\Users\admin\AppData\Local\Temp\other_workspace\clip.mp4", "out.mp4"),
            cwd=r"C:\Users\admin\Videos",
            create_time=904.0,
        ),
    ]


def test_split_windows_command_line_handles_spaces_in_paths():
    cmd = r'"C:\tool v1\backend\venv\Scripts\pythonw.exe" "C:\tool v1\backend\background_service.py" --service telegram'
    assert split_windows_command_line(cmd) == [
        r"C:\tool v1\backend\venv\Scripts\pythonw.exe",
        r"C:\tool v1\backend\background_service.py",
        "--service",
        "telegram",
    ]


def test_select_targets_excludes_tool_v10_powershell_editor_and_unrelated_ffmpeg(tmp_path):
    project_root = tmp_path / "tool v1"
    records = _fixture_processes(project_root)

    selected_all, _ = select_shutdown_targets(
        records,
        project_root=project_root,
        scope="all",
        include_downloader=False,
        caller_pid=99999,
    )
    pids_all = [item.record.pid for item in selected_all]

    # Legitimate Tool V1 services & workers are selected
    assert {1001, 1002, 1003, 1004, 1010, 1011, 1012, 1030, 1031}.issubset(set(pids_all))
    # Standalone downloader is NOT implicitly stopped in scope="all" unless requested
    assert 1020 not in pids_all
    # Non-targets (C:\tool v10, PowerShell, Editor, other Python, other ffmpeg with temp/workspace) are NEVER selected
    for forbidden_pid in (9001, 9002, 9003, 9004, 9005):
        assert forbidden_pid not in pids_all

    # Supervisors are ordered before workers so they cannot respawn children
    first_worker_idx = next(i for i, item in enumerate(selected_all) if not item.is_supervisor)
    last_supervisor_idx = max(i for i, item in enumerate(selected_all) if item.is_supervisor)
    assert last_supervisor_idx < first_worker_idx


def test_stop_bot_scope_only_selects_telegram_and_its_supervisor(tmp_path):
    project_root = tmp_path / "tool v1"
    records = _fixture_processes(project_root)

    selected_tg, _ = select_shutdown_targets(
        records,
        project_root=project_root,
        scope="telegram",
        caller_pid=99999,
    )
    pids_tg = {item.record.pid for item in selected_tg}
    assert pids_tg == {1001, 1002, 1003, 1004}


def test_telemetry_pid_reuse_and_verification(tmp_path):
    project_root = tmp_path / "tool v1"
    records = _fixture_processes(project_root)
    by_pid = {r.pid: r for r in records}

    # 1. Reused PID pointing to unrelated process (e.g. PowerShell 9002 or other python 9004)
    pid, reason = verify_telemetry_pid(
        {"pid": 9004, "at": 2000.0},
        by_pid,
        project_root,
    )
    assert pid is None
    assert "role_mismatch" in reason

    # 2. Reused PID where process was created AFTER telemetry timestamp
    pid2, reason2 = verify_telemetry_pid(
        {"pid": 1003, "at": 500.0},
        by_pid,
        project_root,
    )
    assert pid2 is None
    assert "created_after_heartbeat" in reason2

    # 3. Valid telemetry PID matching script and creation time
    pid3, reason3 = verify_telemetry_pid(
        {"pid": 1003, "at": 1005.0, "started_at": 1001.0},
        by_pid,
        project_root,
    )
    assert pid3 == 1003
    assert reason3 == "verified_telemetry_pid"


def test_preview_mode_performs_zero_mutations(tmp_path):
    project_root = tmp_path / "tool v1"
    workspace = project_root / "workspace"
    records = _fixture_processes(project_root)

    terminator_mock = MagicMock()
    task_mock = MagicMock()
    port_mock = MagicMock()

    res = execute_shutdown(
        project_root=project_root,
        workspace=workspace,
        scope="all",
        preview=True,
        process_enumerator=lambda: records,
        process_terminator=terminator_mock,
        task_controller=task_mock,
        port_checker=port_mock,
    )

    assert res.exit_code == EXIT_OK
    assert res.preview is True
    assert len(res.selected) == 9
    assert terminator_mock.call_count == 0
    assert task_mock.call_count == 0
    assert port_mock.call_count == 0
    assert res.flags_written == []
    assert not (workspace / "control" / "v1.pause").exists()


def test_busy_guard_blocks_shutdown_unless_forced(tmp_path):
    project_root = tmp_path / "tool v1"
    workspace = project_root / "workspace"
    (workspace / "bot_system").mkdir(parents=True)
    (workspace / "bot_system" / "job_status.json").write_text(
        json.dumps({"active": True, "video_name": "test_clip.mp4"}),
        encoding="utf-8",
    )
    records = _fixture_processes(project_root)
    terminator_mock = MagicMock(return_value=([], [], []))
    task_mock = MagicMock(return_value=[])

    # Default (force=False) refuses with EXIT_BUSY and does not touch tasks or processes
    blocked = execute_shutdown(
        project_root=project_root,
        workspace=workspace,
        scope="all",
        force=False,
        process_enumerator=lambda: records,
        process_terminator=terminator_mock,
        task_controller=task_mock,
        port_checker=lambda _: [],
    )
    assert blocked.exit_code == EXIT_BUSY
    assert "dashboard_job_active:test_clip.mp4" in blocked.busy_reasons
    assert terminator_mock.call_count == 0
    assert task_mock.call_count == 0

    # Explicit force=True proceeds
    state = {"calls": 0}

    def _enum():
        state["calls"] += 1
        return records if state["calls"] == 1 else []

    forced = execute_shutdown(
        project_root=project_root,
        workspace=workspace,
        scope="all",
        force=True,
        process_enumerator=_enum,
        process_terminator=lambda targets: ([t.record.pid for t in targets], [], []),
        task_controller=task_mock,
        port_checker=lambda _: [],
    )
    assert forced.exit_code == EXIT_OK
    assert len(forced.terminated_pids) == 9


def test_process_terminator_handles_nosuchprocess_access_denied_and_pid_reuse():
    import psutil

    rec_gone = SelectedTarget(
        record=ProcessRecord(pid=2001, ppid=1, name="python.exe", create_time=100.0),
        role="worker",
        reason="test",
    )
    rec_denied = SelectedTarget(
        record=ProcessRecord(pid=2002, ppid=1, name="python.exe", exe=r"C:\Python\python.exe",
                             cmdline=(r"C:\Python\python.exe", "worker.py"), create_time=100.0),
        role="worker",
        reason="test",
    )
    rec_reused = SelectedTarget(
        record=ProcessRecord(pid=2003, ppid=1, name="python.exe", create_time=100.0),
        role="worker",
        reason="test",
    )

    def _fake_process(pid):
        if pid == 2001:
            raise psutil.NoSuchProcess(pid)
        proc = MagicMock()
        proc.pid = pid
        if pid == 2002:
            proc.create_time.return_value = 100.0
            proc.exe.return_value = rec_denied.record.exe
            proc.cmdline.return_value = rec_denied.record.cmdline
            proc.terminate.side_effect = psutil.AccessDenied(pid)
        elif pid == 2003:
            # Creation time changed -> PID was reused by OS!
            proc.create_time.return_value = 999.0
        return proc

    with patch("backend.v1_shutdown.psutil.Process", side_effect=_fake_process), patch(
        "backend.v1_shutdown.psutil.wait_procs", return_value=([], [])
    ):
        terminated, killed, denied = default_process_terminator(
            [rec_gone, rec_denied, rec_reused]
        )

    # 2001 (NoSuchProcess) treated as stopped; 2002 reported in denied; 2003 skipped due to PID reuse
    assert terminated == [2001]
    assert killed == []
    assert denied == [2002]


def test_execute_shutdown_reports_access_denied_and_survivors_and_idempotent_second_call(tmp_path):
    project_root = tmp_path / "tool v1"
    workspace = project_root / "workspace"
    records = _fixture_processes(project_root)

    # 1. AccessDenied returns EXIT_ACCESS_DENIED
    res_denied = execute_shutdown(
        project_root=project_root,
        workspace=workspace,
        scope="telegram",
        process_enumerator=lambda: records,
        process_terminator=lambda _: ([], [], [1003]),
        task_controller=lambda *_, **__: [],
        port_checker=lambda _: [],
    )
    assert res_denied.exit_code == EXIT_ACCESS_DENIED
    assert res_denied.access_denied_pids == [1003]

    # 2. Survivor remaining returns EXIT_SURVIVOR
    res_survivor = execute_shutdown(
        project_root=project_root,
        workspace=workspace,
        scope="telegram",
        process_enumerator=lambda: records,  # still alive after terminate
        process_terminator=lambda targets: ([t.record.pid for t in targets], [], []),
        task_controller=lambda *_, **__: [],
        port_checker=lambda _: [],
    )
    assert res_survivor.exit_code == EXIT_SURVIVOR
    assert set(res_survivor.survivor_pids) == {1001, 1002, 1003, 1004}

    # 3. Calling stop a second time when already stopped succeeds cleanly with EXIT_OK
    res_second = execute_shutdown(
        project_root=project_root,
        workspace=workspace,
        scope="telegram",
        process_enumerator=lambda: [r for r in records if r.pid >= 9000],
        process_terminator=lambda _: ([], [], []),
        task_controller=lambda *_, **__: [],
        port_checker=lambda _: [],
    )
    assert res_second.exit_code == EXIT_OK
    assert res_second.terminated_pids == []


def test_stale_telemetry_with_no_live_bot_returns_ambiguous_exit_code(tmp_path):
    project_root = tmp_path / "tool v1"
    workspace = project_root / "workspace"
    control_dir = workspace / "bot_system" / "control"
    control_dir.mkdir(parents=True)
    # Telemetry points to PID 9004 (which is an unrelated Python process)
    (control_dir / "v1.json").write_text(
        json.dumps({"pid": 9004, "at": 1000.0, "polling": True, "busy": False}),
        encoding="utf-8",
    )
    only_unrelated = [r for r in _fixture_processes(project_root) if r.pid >= 9000]

    res = stop_v1_bot(
        project_root=project_root,
        workspace=workspace,
        process_enumerator=lambda: only_unrelated,
        process_terminator=lambda _: ([], [], []),
        task_controller=lambda *_, **__: [],
        port_checker=lambda _: [],
    )
    assert res.exit_code == EXIT_AMBIGUOUS
    assert "role_mismatch" in res.telemetry_rejected_reason


def test_process_killer_only_terminates_verified_workers_not_servers_or_unrelated_ffmpeg(tmp_path):
    project_root = tmp_path / "tool v1"
    records = _fixture_processes(project_root)

    killed_pids = terminate_worker_processes(
        project_root=project_root,
        workspace_roots=[project_root / "workspace"],
        caller_pid=1010,  # called from main.py
        process_enumerator=lambda: records,
        process_terminator=lambda targets, timeout=1.5: ([t.record.pid for t in targets], [], []),
    )

    # Only verified project workers (1030 ffmpeg in project workspace, 1031 v1_ocr_worker) are terminated
    assert set(killed_pids) == {1030, 1031}
    # Host servers (1010 main.py, 1003 telegram_bot.py) and unrelated ffmpeg (9005) are untouched
    for protected in (1001, 1003, 1010, 1011, 9001, 9005):
        assert protected not in killed_pids
