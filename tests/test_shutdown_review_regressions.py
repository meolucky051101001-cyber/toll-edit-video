"""Negative shutdown cases: all processes/tasks are synthetic, all files are temporary."""
import json
import subprocess
from pathlib import Path
from unittest.mock import Mock, patch

import pytest

from backend import v1_shutdown as s
from backend.service_control import control_directories, resume_services, service_paused


def task(root, name="AutoDubV1-dashboard", script="background_service.py", arguments="--service dashboard"):
    backend = root / "backend"
    return s.TaskRecord(name, (s.TaskAction(str(backend / "venv/Scripts/pythonw.exe"),
                                          f'"{backend / script}" {arguments}', str(backend)),))


def execute(root, **kwargs):
    options = dict(project_root=root, process_enumerator=lambda: [],
                   process_terminator=Mock(return_value=([], [], [])),
                   task_controller=Mock(return_value=[]), task_enumerator=lambda: [], port_checker=lambda _: [])
    options.update(kwargs)
    return s.execute_shutdown(**options)


@pytest.mark.parametrize("invalid", ["other-root", "wrong-service", "multiple-actions"])
def test_mismatched_task_never_mutates_flags_tasks_or_processes(tmp_path, invalid):
    root = tmp_path / "project"
    record = task(tmp_path / "production") if invalid == "other-root" else task(root, script="telegram_bot.py", arguments="")
    if invalid == "multiple-actions":
        valid = task(root)
        record = s.TaskRecord(valid.name, valid.actions * 2)
    controller, terminator = Mock(), Mock()
    result = execute(root, task_enumerator=lambda: [record], task_controller=controller, process_terminator=terminator)
    assert result.exit_code == s.EXIT_AMBIGUOUS
    controller.assert_not_called()
    terminator.assert_not_called()
    assert not (root / "workspace").exists()


def test_only_verified_task_is_requested_and_enabled_state_is_not_changed(tmp_path):
    record = task(tmp_path)
    controller = Mock(return_value=[record.name])
    result = execute(tmp_path, task_enumerator=lambda: [record], task_controller=controller)
    assert result.exit_code == s.EXIT_OK
    assert controller.call_args.args[0] == [record.name]
    assert controller.call_args.kwargs["project_root"] == tmp_path
    with patch.object(s.subprocess, "run", return_value=Mock(returncode=0)) as run:
        assert s.default_task_Controller([record.name], mode="stop", project_root=tmp_path,
                                         task_enumerator=lambda: [record]) == [record.name]
    assert "/End" in run.call_args.args[0]
    assert "/Change" not in run.call_args.args[0]


def test_task_inventory_access_denied_never_stops_anything(tmp_path):
    controller = Mock()
    result = execute(tmp_path, task_enumerator=Mock(side_effect=PermissionError("denied")), task_controller=controller)
    assert result.exit_code == s.EXIT_ACCESS_DENIED
    controller.assert_not_called()
    assert not (tmp_path / "workspace").exists()


def test_controller_revalidates_actions_before_end(tmp_path):
    with patch.object(s.subprocess, "run") as run:
        with pytest.raises(s.TaskVerificationError):
            s.default_task_Controller(["AutoDubV1-dashboard"], mode="stop", project_root=tmp_path,
                                      task_enumerator=lambda: [task(tmp_path / "different")])
        run.assert_not_called()


def test_ready_task_is_not_ended_and_already_exited_race_is_verified(tmp_path):
    running = task(tmp_path)
    ready = s.TaskRecord(running.name, running.actions, running=False)
    with patch.object(s.subprocess, "run") as run:
        assert s.default_task_Controller([ready.name], mode="stop", project_root=tmp_path,
                                         task_enumerator=lambda: [ready]) == []
        run.assert_not_called()
    inventory = iter([[running], [ready]])
    with patch.object(s.subprocess, "run", return_value=Mock(returncode=1)):
        assert s.default_task_Controller([running.name], mode="stop", project_root=tmp_path,
                                         task_enumerator=lambda: next(inventory)) == []


def test_powershell_task_reader_preserves_array_and_action_metadata():
    # Shadow the cmdlet in a child PowerShell session, not on the host.
    # This catches ConvertFrom-Json's nested-array behavior in Windows PowerShell 5.1.
    real_run = subprocess.run
    fake_cmdlet = r"""
function Get-ScheduledTask {
  param($ErrorAction)
  [pscustomobject]@{TaskName='AutoDub-control';TaskPath='\';State='Running';Actions=@(
    [pscustomobject]@{Execute='C:\fixture\backend\venv\Scripts\pythonw.exe';Arguments='C:\fixture\backend\tool_control.py';WorkingDirectory='C:\fixture\backend'}
  )}
}
"""
    def child(command, **kwargs):
        command = list(command)
        command[-1] = fake_cmdlet + command[-1]
        return real_run(command, **kwargs)
    with patch.object(s.subprocess, "run", side_effect=child):
        records = s.enumerate_scheduled_tasks()
    assert len(records) == 1
    assert records[0].name == "\\AutoDub-control"
    assert records[0].running is True
    assert records[0].actions[0].arguments == r"C:\fixture\backend\tool_control.py"


@pytest.mark.parametrize("script,cwd,exe,created", [
    ("main.py", "", "external", 100),
    ("other-app/main.py", "root", "external", 100),
    ("backend/main.py", "root", "", 100),
    ("backend/main.py", "root", "external", 0),
])
def test_incomplete_or_wrong_script_identity_is_never_selected(tmp_path, script, cwd, exe, created):
    python = str(tmp_path.parent / "python.exe") if exe else ""
    record = s.ProcessRecord(401, 1, "python.exe", python, (python or "python", script),
                             str(tmp_path) if cwd else "", created)
    selected, _ = s.select_shutdown_targets([record], project_root=tmp_path, caller_pid=9999)
    assert selected == []


@pytest.mark.parametrize("delta,changed_exe,changed_argv", [(0.001, False, False), (1, False, False),
                                                         (0, True, False), (0, False, True)])
def test_identity_is_revalidated_before_terminate(tmp_path, delta, changed_exe, changed_argv):
    exe = str(tmp_path / "python.exe")
    argv = (exe, str(tmp_path / "backend/main.py"))
    record = s.ProcessRecord(402, 1, "python.exe", exe, argv, str(tmp_path), 100)
    live = Mock(pid=402)
    live.create_time.return_value = 100 + delta
    live.exe.return_value = str(tmp_path / "other.exe") if changed_exe else exe
    live.cmdline.return_value = (exe, "other.py") if changed_argv else argv
    with patch.object(s.psutil, "Process", return_value=live):
        outcome = s.default_process_terminator([s.SelectedTarget(record, "dashboard", "test")])
    live.terminate.assert_not_called()
    live.kill.assert_not_called()
    assert outcome.identity_mismatches == [402]


def test_dotenv_only_workspace_active_job_blocks_stop(tmp_path, monkeypatch):
    monkeypatch.delenv("AUTODUB_WORKSPACE", raising=False)
    (tmp_path / "backend").mkdir()
    ws = tmp_path / "configured"
    (ws / "bot_system").mkdir(parents=True)
    (tmp_path / "backend/.env").write_text(f'AUTODUB_WORKSPACE="{ws}"\n', encoding="utf-8")
    (ws / "bot_system/job_status.json").write_text('{"active":true}', encoding="utf-8")
    terminator = Mock()
    result = execute(tmp_path, process_terminator=terminator)
    assert result.exit_code == s.EXIT_BUSY
    terminator.assert_not_called()


def test_corrupt_status_does_not_mean_idle(tmp_path):
    ws = tmp_path / "workspace"
    (ws / "bot_system").mkdir(parents=True)
    (ws / "bot_system/job_status.json").write_text("partial-json", encoding="utf-8")
    result = execute(tmp_path, workspace=ws)
    assert result.exit_code == s.EXIT_BUSY


def test_identity_mismatch_is_reported_without_clearing_live_telemetry(tmp_path):
    ws = tmp_path / "workspace"
    control = ws / "control"
    control.mkdir(parents=True)
    payload = {"pid": 401, "at": 100, "create_time": 100, "polling": True}
    telemetry = control / "v1.json"
    telemetry.write_text(json.dumps(payload), encoding="utf-8")
    exe = str(tmp_path / "backend/venv/Scripts/python.exe")
    record = s.ProcessRecord(401, 1, "python.exe", exe,
                             (exe, str(tmp_path / "backend/telegram_bot.py")), str(tmp_path), 100)
    result = execute(tmp_path, workspace=ws, scope="telegram", process_enumerator=lambda: [record],
                     process_terminator=lambda _: s.TerminationResult(identity_mismatches=[401]))
    assert result.exit_code == s.EXIT_AMBIGUOUS
    assert json.loads(telemetry.read_text(encoding="utf-8")) == payload


@pytest.mark.parametrize("scope", ["all", "telegram", "downloader"])
def test_stop_resume_round_trip_across_all_control_layouts(tmp_path, monkeypatch, scope):
    ws = tmp_path / "configured"
    override = tmp_path / "custom-control"
    monkeypatch.setenv("TOOL_V1_CONTROL_DIR", str(override))
    result = execute(tmp_path, workspace=ws, scope=scope)
    assert result.exit_code == s.EXIT_OK
    directories = control_directories(tmp_path, ws)
    for directory in directories:
        # Independent downloader intent is deliberately left stopped by an all/V1 start.
        if scope != "downloader":
            (directory / "downloader.disabled").write_text("paused", encoding="utf-8")
    resume_services(tmp_path, workspace=ws, scope=scope)
    assert all(not Path(path).exists() for path in result.flags_written)
    if scope != "downloader":
        assert all((directory / "downloader.disabled").exists() for directory in directories)
    assert not service_paused(tmp_path, workspace=ws, service="telegram")


def test_restart_mode_does_not_create_persistent_intents(tmp_path):
    result = execute(tmp_path, mode="restart")
    assert result.exit_code == s.EXIT_OK
    assert result.flags_written == []


def test_start_launchers_use_shared_resume_and_nested_cli_keeps_main():
    root = Path(__file__).resolve().parents[1]
    for file in ("start_bot.bat", "start_telebot_downloader.bat"):
        assert "--resume" in (root / file).read_text(encoding="utf-8")
