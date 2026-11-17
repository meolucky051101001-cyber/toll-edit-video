"""Exercise production bootstrap functions without loading local credentials."""
import ast
import logging
import threading
from pathlib import Path
from types import SimpleNamespace
from typing import Mapping
from unittest.mock import Mock, call

import pytest


ROOT = Path(__file__).resolve().parents[1]
PAUSE_NAMES = (
    "v1.pause", "ops.pause", "telegram.disabled", "scriptbot.disabled",
    "video.pause", "video.pause.ack",
)


def load_functions(filename, names, namespace):
    tree = ast.parse((ROOT / "backend" / filename).read_text(encoding="utf-8-sig"))
    functions = [node for node in tree.body
                 if isinstance(node, ast.FunctionDef) and node.name in names]
    assert {node.name for node in functions} == set(names)
    exec(compile(ast.Module(body=functions, type_ignores=[]), filename, "exec"), namespace)


@pytest.fixture
def controller(tmp_path):
    directories = [tmp_path / name for name in (
        "custom-control", "workspace/bot_system/control", "workspace/control",
        "project/workspace/bot_system/control", "project/workspace/control",
    )]
    namespace = {
        "Path": Path,
        "Mapping": Mapping,
        "ROOTS": {"v1": tmp_path / "project/backend"},
        "_STOP_OPERATIONS": {},
        "_STOP_OPERATIONS_LOCK": threading.Lock(),
        "control_directories": lambda *args, **kwargs: directories,
        "get_control_paths": lambda key, name: [path / name for path in directories],
        "processes": Mock(return_value=[]),
        "launch": Mock(),
        "logger": logging.getLogger(__name__),
        "time": SimpleNamespace(time=lambda: 100, sleep=Mock()),
        "read_telemetry": Mock(return_value={"pid": 7, "at": 100, "polling": True}),
        "status": Mock(return_value={"state": "running"}),
    }
    load_functions("service_control.py", ("intent_names", "resume_services"), namespace)
    load_functions("tool_control.py", ("apply_boot_defaults", "change"), namespace)
    return SimpleNamespace(directories=directories, namespace=namespace)


@pytest.mark.parametrize("name", PAUSE_NAMES)
@pytest.mark.parametrize("location", range(5))
def test_automatic_boot_preserves_every_pause_location(controller, name, location):
    flag = controller.directories[location] / name
    flag.parent.mkdir(parents=True)
    flag.write_text("operator pause", encoding="utf-8")
    ns = controller.namespace
    ns["apply_boot_defaults"]()
    ns["launch"].assert_called_once_with("v1", "dashboard")
    assert flag.read_text(encoding="utf-8") == "operator pause"


def test_fresh_boot_starts_dashboard_and_telegram(controller):
    ns = controller.namespace
    ns["apply_boot_defaults"]()
    assert ns["launch"].call_args_list == [call("v1", "dashboard"), call("v1", "telegram")]
    assert not any(path.exists() for path in controller.directories)


def test_boot_does_not_duplicate_existing_services(controller):
    ns = controller.namespace
    ns["processes"].return_value = [
        (SimpleNamespace(pid=6), "main.py"),
        (SimpleNamespace(pid=7), "telegram_bot.py"),
    ]
    ns["apply_boot_defaults"]()
    ns["launch"].assert_not_called()


def test_explicit_start_clears_pause_and_resumes_telegram(controller):
    for directory in controller.directories:
        directory.mkdir(parents=True)
        for name in (*PAUSE_NAMES, "downloader.disabled"):
            (directory / name).write_text("operator pause", encoding="utf-8")
    ns = controller.namespace
    ns["processes"].side_effect = [[], [(SimpleNamespace(pid=7), "telegram_bot.py")]]
    ns["change"]("v1", "on")
    assert ns["launch"].call_args_list == [call("v1", "dashboard"), call("v1", "telegram")]
    assert all(not (path / name).exists() for path in controller.directories for name in PAUSE_NAMES)
    assert all((path / "downloader.disabled").is_file() for path in controller.directories)
