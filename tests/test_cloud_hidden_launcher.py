"""Hidden bootstrap uses only a pinned runtime and verified startup reports."""
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

import launch_cloud_dashboard as launcher


@pytest.fixture
def checkout(tmp_path):
    root = tmp_path / "checkout with spaces"
    executable = root / "backend/venv/Scripts/python.exe"
    executable.parent.mkdir(parents=True)
    executable.write_bytes(b"synthetic-runtime")
    (root / "run_cloud_node.py").write_text("# synthetic runner", encoding="utf-8")
    return root


def test_missing_runtime_never_uses_generic_python(checkout, monkeypatch):
    executable = checkout / "backend/venv/Scripts/python.exe"
    executable.unlink()
    spawn, message = Mock(), Mock()
    monkeypatch.setattr(launcher.subprocess, "Popen", spawn)
    monkeypatch.setattr(launcher, "show_startup_error", message)
    assert launcher.launch_dashboard(root=checkout, environment={}) == 1
    spawn.assert_not_called()
    message.assert_called_once_with(checkout / "workspace/cloud_desktop/logs")


def test_hidden_spawn_preserves_paths_redirects_logs_and_waits(checkout, tmp_path, monkeypatch):
    override = tmp_path / "runtime with spaces/python.exe"
    override.parent.mkdir()
    override.write_bytes(b"synthetic-override")
    workspace = tmp_path / "separate workspace"
    observed = {}
    process = SimpleNamespace(pid=127, poll=Mock(return_value=None), kill=Mock(), terminate=Mock())

    def spawn(command, **kwargs):
        observed.update(command=command, kwargs=kwargs,
                        stdout=kwargs["stdout"].name, stderr=kwargs["stderr"].name)
        assert kwargs["stdout"].mode == "ab"
        assert kwargs["stderr"].mode == "ab"
        report_path = Path(command[command.index("--startup-report") + 1])
        report_path.write_text(json.dumps({"status": "ready", "pid": process.pid, "code": 0}), encoding="utf-8")
        return process

    message = Mock()
    monkeypatch.setattr(launcher.subprocess, "Popen", spawn)
    monkeypatch.setattr(launcher, "show_startup_error", message)
    environment = {"AUTODUB_CLOUD_PYTHON": str(override), "AUTODUB_WORKSPACE": str(workspace),
                   "SYNTHETIC_SECRET": "fixture-do-not-log"}
    assert launcher.launch_dashboard(root=checkout, environment=environment, platform_name="nt") == 0
    command, kwargs = observed["command"], observed["kwargs"]
    assert command[:9] == [str(override), "-B", "-X", "utf8", "-u", str(checkout / "run_cloud_node.py"),
                           "--port", "8089", "--no-worker"]
    assert "fixture-do-not-log" not in " ".join(command)
    assert kwargs["cwd"] == checkout
    assert kwargs["env"] == environment
    assert kwargs["stdin"] == launcher.subprocess.DEVNULL
    assert kwargs["creationflags"] == 0x08000200
    assert kwargs["close_fds"] is True
    assert Path(observed["stdout"]).parent == workspace / "cloud_desktop/logs"
    assert observed["stdout"] != observed["stderr"]
    assert Path(observed["stdout"]).read_bytes() == b""
    assert Path(observed["stderr"]).read_bytes() == b""
    message.assert_not_called()
    process.kill.assert_not_called()
    process.terminate.assert_not_called()


def test_ready_report_accepts_quick_duplicate_exit(tmp_path):
    report = tmp_path / "startup.json"
    report.write_text(json.dumps({"status": "ready", "pid": 42, "code": 0}), encoding="utf-8")
    process = SimpleNamespace(pid=42, poll=Mock(return_value=0), kill=Mock(), terminate=Mock())
    assert launcher.wait_for_startup(process, report)
    process.kill.assert_not_called()
    process.terminate.assert_not_called()


def test_ready_report_does_not_hide_nonzero_child_exit(tmp_path):
    report = tmp_path / "startup.json"
    report.write_text(json.dumps({"status": "ready", "pid": 42, "code": 0}), encoding="utf-8")
    process = SimpleNamespace(pid=42, returncode=1, poll=Mock(return_value=1), kill=Mock(), terminate=Mock())
    assert not launcher.wait_for_startup(process, report)
    process.kill.assert_not_called()
    process.terminate.assert_not_called()


def test_report_arriving_during_failed_exit_cannot_claim_ready(tmp_path, monkeypatch):
    report = tmp_path / "startup.json"
    process = SimpleNamespace(pid=42, returncode=1, poll=Mock(return_value=1))
    monkeypatch.setattr(launcher, "read_startup_report", Mock(side_effect=[None, "ready"]))
    assert not launcher.wait_for_startup(process, report)


@pytest.mark.parametrize("report_value", [None, {"status": "failed", "pid": 42, "code": 1},
                                          {"status": "ready", "pid": 99, "code": 0},
                                          {"status": "ready", "pid": 99, "ppid": 88, "code": 0},
                                          {"status": "ready", "pid": True, "code": 0},
                                          {"status": "ready", "pid": 42, "code": False}])
def test_import_failure_or_invalid_report_never_claims_ready(tmp_path, report_value):
    report = tmp_path / "startup.json"
    if report_value is not None:
        report.write_text(json.dumps(report_value), encoding="utf-8")
    process = SimpleNamespace(pid=42, returncode=1, poll=Mock(return_value=1), kill=Mock(), terminate=Mock())
    assert not launcher.wait_for_startup(process, report)
    process.kill.assert_not_called()
    process.terminate.assert_not_called()


@pytest.mark.parametrize("poll_result", [None, 0])
def test_windows_venv_redirector_child_report_matches_spawned_parent_pid(checkout, monkeypatch, poll_result):
    """Windows venv Scripts/python.exe (pid=23740) spawns base python.exe (pid=24672, ppid=23740)."""
    process = SimpleNamespace(
        pid=23740,
        returncode=0 if poll_result == 0 else None,
        poll=Mock(return_value=poll_result),
        kill=Mock(),
        terminate=Mock(),
    )

    def spawn(command, **kwargs):
        report_path = Path(command[command.index("--startup-report") + 1])
        report_path.write_text(
            json.dumps({"status": "ready", "code": 0, "pid": 24672, "ppid": 23740}),
            encoding="utf-8",
        )
        return process

    message = Mock()
    monkeypatch.setattr(launcher.subprocess, "Popen", spawn)
    monkeypatch.setattr(launcher, "show_startup_error", message)
    assert launcher.launch_dashboard(root=checkout, environment={}, platform_name="nt") == 0
    message.assert_not_called()
    process.kill.assert_not_called()
    process.terminate.assert_not_called()


def test_windows_venv_redirector_failed_report_stops_waiting_immediately(tmp_path):
    report = tmp_path / "startup.json"
    report.write_text(
        json.dumps({"status": "failed", "code": 2, "pid": 24672, "ppid": 23740}),
        encoding="utf-8",
    )
    process = SimpleNamespace(pid=23740, returncode=2, poll=Mock(return_value=None))
    assert not launcher.wait_for_startup(process, report, timeout=5.0)


def test_timeout_leaves_process_alive_and_reports_generic_error(checkout, monkeypatch):
    process = SimpleNamespace(pid=42, poll=Mock(return_value=None), kill=Mock(), terminate=Mock())
    message = Mock()
    monkeypatch.setattr(launcher.subprocess, "Popen", Mock(return_value=process))
    monkeypatch.setattr(launcher, "show_startup_error", message)
    assert launcher.launch_dashboard(root=checkout, environment={}, timeout=0) == 1
    message.assert_called_once_with(checkout / "workspace/cloud_desktop/logs")
    process.kill.assert_not_called()
    process.terminate.assert_not_called()


def test_pythonw_missing_standard_streams_do_not_change_result(checkout, monkeypatch):
    # Bootstrap never prints, writes stderr, or requires a console handle.
    monkeypatch.setattr(launcher.subprocess, "Popen", Mock(side_effect=OSError("fixture-private-error")))
    message = Mock()
    monkeypatch.setattr(launcher, "show_startup_error", message)
    assert launcher.launch_dashboard(root=checkout, environment={}) == 1
    assert len(message.call_args.args) == 1
    assert isinstance(message.call_args.args[0], Path)
