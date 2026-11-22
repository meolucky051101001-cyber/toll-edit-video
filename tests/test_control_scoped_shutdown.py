"""Control stop uses synthetic processes/tasks and temporary files only."""
import io
import json
import os
from pathlib import Path
from unittest.mock import Mock, patch

import pytest
import tool_control as control
from backend import v1_shutdown as shutdown
from backend import voice_selection
from backend.service_control import intent_names


def process(root, pid, script, *, ppid=1, args=()):
    exe = str(root / "backend/venv/Scripts/python.exe")
    return shutdown.ProcessRecord(pid, ppid, "python.exe", exe,
                                  (exe, str(root / script), *args), str(root), float(pid))


@pytest.fixture
def local_root(tmp_path, monkeypatch):
    root = tmp_path / "project"
    root.mkdir()
    monkeypatch.setenv("AUTODUB_WORKSPACE", str(root / "workspace"))
    monkeypatch.delenv("TOOL_V1_CONTROL_DIR", raising=False)
    monkeypatch.delenv("AUTODUB_CONTROL_DIR", raising=False)
    monkeypatch.setitem(control.ROOTS, "v1", root / "backend")
    return root


def test_processing_targets_preserve_hosts_downloader_and_foreign_media(local_root):
    root = local_root
    records = [
        process(root, 10, "backend/main.py"),
        process(root, 11, "backend/tool_control.py"),
        process(root, 12, "backend/ops_watchdog.py"),
        process(root, 13, "backend/background_service.py", args=("--service", "dashboard")),
        process(root, 20, "backend/background_service.py", args=("--service", "telegram")),
        process(root, 21, "backend/telegram_bot.py", ppid=20),
        process(root, 22, "backend/telegram_script_bot.py"),
        process(root, 23, "backend/model_workers/v1_ocr_worker.py", ppid=21),
        process(root, 30, "telebot_video_downloader.py", ppid=21),
        shutdown.ProcessRecord(31, 30, "ffmpeg.exe", str(root / "bin/ffmpeg.exe"),
                               ("ffmpeg", "-i", str(root / "download.mp4")), str(root), 31),
        shutdown.ProcessRecord(40, 1, "ffmpeg.exe", str(root.parent / "ffmpeg.exe"),
                               ("ffmpeg", "-i", str(root / "unrelated.mp4")), str(root.parent), 40),
        shutdown.ProcessRecord(24, 21, "ffmpeg.exe", str(root / "bin/ffmpeg.exe"),
                               ("ffmpeg", "-i", str(root / "source.mp4")), str(root), 24),
    ]
    selected, _ = shutdown.select_shutdown_targets(
        records, project_root=root, scope="processing", caller_pid=999,
    )
    assert {item.record.pid for item in selected} == {20, 21, 22, 23, 24}


def test_processing_busy_does_not_write_flags_stop_tasks_or_processes(local_root):
    workspace = local_root / "workspace"
    status = workspace / "bot_system/job_status.json"
    status.parent.mkdir(parents=True)
    status.write_text('{"active":true,"job_id":"fixture"}', encoding="utf-8")
    terminator, tasks = Mock(), Mock()
    result = shutdown.execute_shutdown(
        project_root=local_root, workspace=workspace, scope="processing",
        process_enumerator=lambda: [process(local_root, 21, "backend/telegram_bot.py")],
        process_terminator=terminator, task_controller=tasks,
        task_enumerator=lambda: [], port_checker=Mock(),
    )
    assert result.exit_code == shutdown.EXIT_BUSY
    assert not result.flags_written
    assert not list(workspace.rglob("*.pause"))
    terminator.assert_not_called()
    tasks.assert_not_called()


def test_processing_stop_checks_only_processing_tasks_and_keeps_ui_ports(local_root):
    def task(name, script, args=""):
        backend = local_root / "backend"
        return shutdown.TaskRecord(name, (shutdown.TaskAction(
            str(backend / "venv/Scripts/python.exe"), f'"{backend / script}" {args}', str(backend)),))
    records = [process(local_root, 20, "backend/background_service.py", args=("--service", "telegram")),
               process(local_root, 21, "backend/telegram_bot.py", ppid=20)]
    inventory = iter([records, []])
    controller = Mock(return_value=["AutoDubV1-telegram"])
    ports = Mock()
    result = shutdown.execute_shutdown(
        project_root=local_root, scope="processing", caller_pid=999,
        process_enumerator=lambda: next(inventory),
        process_terminator=lambda selected: shutdown.TerminationResult([item.record.pid for item in selected]),
        task_controller=controller,
        task_enumerator=lambda: [task("AutoDubV1-telegram", "background_service.py", "--service telegram"),
                                 task("AutoDub-control", "tool_control.py"),
                                 task("AutoDubV1-dashboard", "main.py")],
        port_checker=ports,
    )
    assert result.exit_code == shutdown.EXIT_OK
    assert controller.call_args.args[0] == ["AutoDubV1-telegram"]
    assert {Path(p).name for p in result.flags_written} == {
        "v1.pause", "ops.pause", "telegram.disabled", "scriptbot.disabled"}
    assert not list((local_root / "workspace").rglob("downloader.disabled"))
    ports.assert_not_called()


@pytest.mark.parametrize("code", [shutdown.EXIT_BUSY, shutdown.EXIT_ACCESS_DENIED,
                                  shutdown.EXIT_AMBIGUOUS, shutdown.EXIT_SURVIVOR])
def test_control_propagates_shutdown_failure_without_fallback_kill(local_root, code):
    result = control.shutdown.ShutdownResult(code, "processing", "stop", False)
    with patch.object(control.shutdown, "execute_shutdown", return_value=result) as stop, \
         patch.object(control.psutil, "process_iter") as processes, \
         patch.object(control.urllib.request, "urlopen") as http:
        with pytest.raises(control.ControlShutdownError) as error:
            control.change("v1", "off")
    assert error.value.result is result
    assert stop.call_args.kwargs == {
        "project_root": local_root, "scope": "processing", "mode": "stop",
        "force": False, "include_downloader": False, "caller_pid": os.getpid(),
    }
    processes.assert_not_called()
    http.assert_not_called()


def test_control_reports_verified_success_without_resource_claim(local_root):
    result = control.shutdown.ShutdownResult(shutdown.EXIT_OK, "processing", "stop", False)
    with patch.object(control.shutdown, "execute_shutdown", return_value=result):
        message = control.change("v1", "off_all")
    assert "Dashboard và Control vẫn mở" in message
    assert "100%" not in message
    assert "RAM" not in message


def test_http_busy_response_is_not_success(local_root):
    result = control.shutdown.ShutdownResult(shutdown.EXIT_BUSY, "processing", "stop", False)
    payload = json.dumps({"key": "v1", "action": "off"}).encode()
    handler = object.__new__(control.Handler)
    handler.path = "/control"
    handler.headers = {"Host": "127.0.0.1:8090", "X-Control-Token": control.TOKEN,
                       "Content-Length": str(len(payload))}
    handler.rfile = io.BytesIO(payload)
    handler.send = Mock()
    with patch.object(control.shutdown, "execute_shutdown", return_value=result):
        handler.do_POST()
    code, body = handler.send.call_args.args
    assert code == 409
    assert body["shutdown"]["exit_code"] == shutdown.EXIT_BUSY


def test_occupied_controller_port_never_terminates_listener():
    with patch("socket.socket") as socket, \
         patch.object(control.urllib.request, "urlopen", side_effect=OSError("not healthy")), \
         patch.object(control.psutil, "Process") as process_lookup:
        socket.return_value.__enter__.return_value.connect_ex.return_value = 0
        assert control.ensure_single_controller() == "occupied"
    process_lookup.assert_not_called()


def test_voice_storage_honors_explicit_empty_workspace(local_root, monkeypatch):
    requested = local_root / "fresh-workspace"
    monkeypatch.setenv("AUTODUB_WORKSPACE", str(requested))
    assert voice_selection._resolve_control_dir() == requested / "bot_system/control"
    assert not requested.exists()


def test_controller_paths_stay_in_current_checkout(local_root):
    assert all(path.is_relative_to(local_root) for path in control.get_control_paths("v1", "v1.pause"))


def test_processing_scope_never_disables_independent_downloader():
    assert "downloader.disabled" not in intent_names("processing", include_downloader=True)


def test_pause_intent_does_not_report_stopped_while_worker_survives(local_root):
    pause = control.get_control_paths("v1", "v1.pause")[0]
    pause.parent.mkdir(parents=True)
    pause.write_text("paused", encoding="utf-8")
    worker = Mock(pid=123)
    with patch.object(control, "processes", return_value=[(worker, "worker")]), \
         patch.object(control, "read_telemetry", return_value={}), \
         patch.object(control.urllib.request, "urlopen", side_effect=OSError("test")):
        result = control.status("v1")
    assert result["paused"] is True
    assert result["state"] == "partial"


def test_status_inventory_does_not_include_downloader_media_child(local_root):
    records = [process(local_root, 10, "backend/main.py"),
               process(local_root, 30, "telebot_video_downloader.py"),
               shutdown.ProcessRecord(31, 30, "ffmpeg.exe", str(local_root / "bin/ffmpeg.exe"),
                                       ("ffmpeg", "-i", str(local_root / "download.mp4")), str(local_root), 31)]
    with patch.object(control.shutdown, "enumerate_live_processes", return_value=records), \
         patch.object(control.psutil, "Process", side_effect=lambda pid: Mock(pid=pid)):
        assert [(p.pid, name) for p, name in control.processes("v1")] == [(10, "main.py")]


def test_status_polls_reuse_recent_process_inventory(local_root, monkeypatch):
    monkeypatch.setattr(control, "_PROCESS_SNAPSHOTS", {})
    monkeypatch.setattr(control, "_SNAPSHOT_REFRESHING", set())
    bot = Mock(pid=77)
    with patch.object(control, "processes", return_value=[(bot, "telegram_bot.py")]) as inventory,          patch.object(control, "read_telemetry", return_value={"pid": 77, "at": control.time.time(), "polling": True}),          patch.object(control.urllib.request, "urlopen", side_effect=OSError("offline")):
        first = control.status("v1", cached=True)
        second = control.status("v1", cached=True)
    assert inventory.call_count == 1
    assert first["bot"] is True and second["heartbeat"] is True


def test_stale_inventory_is_served_while_single_refresh_runs(local_root, monkeypatch):
    monkeypatch.setattr(control, "_PROCESS_SNAPSHOTS", {"v1": (control.time.monotonic() - 60, [(5, "telegram_bot.py")])})
    monkeypatch.setattr(control, "_SNAPSHOT_REFRESHING", set())
    started = []
    monkeypatch.setattr(control.threading, "Thread",
                        lambda target, args, daemon: Mock(start=lambda: started.append(args)))
    with patch.object(control, "processes") as inventory:
        assert control.process_snapshot("v1") == [(5, "telegram_bot.py")]
        assert control.process_snapshot("v1") == [(5, "telegram_bot.py")]
    inventory.assert_not_called()
    assert started == [("v1",)]


def test_refresh_replaces_snapshot_and_releases_single_flight(local_root, monkeypatch):
    monkeypatch.setattr(control, "_PROCESS_SNAPSHOTS", {})
    monkeypatch.setattr(control, "_SNAPSHOT_REFRESHING", {"v1"})
    with patch.object(control, "processes", return_value=[(Mock(pid=9), "main.py")]):
        control._refresh_process_snapshot("v1")
    assert control._PROCESS_SNAPSHOTS["v1"][1] == [(9, "main.py")]
    assert "v1" not in control._SNAPSHOT_REFRESHING


def test_explicit_status_checks_stay_fresh(local_root, monkeypatch):
    monkeypatch.setattr(control, "_PROCESS_SNAPSHOTS", {"v1": (control.time.monotonic(), [(5, "telegram_bot.py")])})
    with patch.object(control, "processes", return_value=[]) as inventory,          patch.object(control, "read_telemetry", return_value={}),          patch.object(control.urllib.request, "urlopen", side_effect=OSError("offline")):
        result = control.status("v1")
    inventory.assert_called_once_with("v1")
    assert result["bot"] is False


def test_health_endpoint_answers_without_process_inventory(local_root):
    import threading
    import urllib.request
    from http.server import ThreadingHTTPServer

    server = ThreadingHTTPServer(("127.0.0.1", 0), control.Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        request = urllib.request.Request(f"http://127.0.0.1:{server.server_port}/health",
                                         headers={"Host": "127.0.0.1:8090"})
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with patch.object(control, "processes", side_effect=AssertionError("inventory used")),              opener.open(request, timeout=5) as response:
            payload = json.load(response)
    finally:
        server.shutdown()
        server.server_close()
    assert response.status == 200
    assert payload == {"ok": True, "pid": os.getpid(), "tools": len(control.ROOTS)}
