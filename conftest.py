import atexit
import importlib
import importlib.abc
import importlib.util
import os
import shutil
import sys
import tempfile
from pathlib import Path

root = Path(__file__).resolve().parent
backend_dir = root / "backend"

# Tiến trình con (subprocess) không nhận patch của fixture, nên chặn từ gốc:
# mọi module đọc AUTODUB_WORKSPACE lúc import và mọi subprocess đều thấy thư mục tạm.
_TEST_WORKSPACE = Path(tempfile.mkdtemp(prefix="autodub_test_ws_"))
os.environ["AUTODUB_WORKSPACE"] = str(_TEST_WORKSPACE)
for _name, _relative in {
    "AUTODUB_DATA_DIR": "data",
    "AUTODUB_INPUT_DIR": "input",
    "AUTODUB_OUTPUT_DIR": "output",
    "AUTODUB_SHARED_WORKSPACE_DIR": "shared",
    "AUTODUB_DELIVERY_DIR": "delivery",
    "SAVE_DIRECTORY": "downloads",
    "TOOL_V1_CONTROL_DIR": "control",
    "AUTODUB_CONTROL_DIR": "control",
    "TOOL_V2_RENDER_HISTORY": "bot_system/.render_history_v2.json",
}.items():
    os.environ[_name] = str(_TEST_WORKSPACE / _relative)
atexit.register(shutil.rmtree, _TEST_WORKSPACE, True)
if str(backend_dir) not in sys.path:
    sys.path.insert(0, str(backend_dir))
if str(root) not in sys.path:
    sys.path.insert(0, str(root))

import pytest
from unittest.mock import patch


def pytest_collection_finish(session):
    if Path(os.environ.get("AUTODUB_WORKSPACE", "")).resolve() != _TEST_WORKSPACE.resolve():
        raise pytest.UsageError("Imports changed AUTODUB_WORKSPACE; refusing tests against a live workspace")


# Catch accidental hardcoded paths and late overrides too. This is installed
# before test collection, so even import-time writes through the workspace
# junction cannot touch production data. Normal temporary fixture paths work.
_LIVE_WORKSPACES = tuple({(root / "workspace").resolve(), Path("D:/workspace").resolve()})


def _reject_live_write(path):
    if not isinstance(path, (str, bytes, os.PathLike)):
        return
    candidate = Path(os.fsdecode(path)).resolve()
    if any(candidate == live or candidate.is_relative_to(live) for live in _LIVE_WORKSPACES):
        raise PermissionError("Tests must not modify the live workspace")


def _test_file_audit(event, args):
    if event == "open":
        path, mode, flags = args
        if (mode and any(c in mode for c in "wax+")) or flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND):
            _reject_live_write(path)
    elif event in {"os.remove", "os.rmdir", "os.mkdir", "os.chmod", "os.utime", "os.truncate"}:
        _reject_live_write(args[0])
    elif event in {"os.rename", "os.link", "os.symlink"}:
        _reject_live_write(args[0])
        _reject_live_write(args[1])


sys.addaudithook(_test_file_audit)


class _BackendAliasLoader(importlib.abc.Loader):
    def __init__(self, target: str):
        self._target = target
        self._original_spec = None

    def create_module(self, spec):
        module = importlib.import_module(self._target)
        self._original_spec = getattr(module, "__spec__", None)
        return module

    def exec_module(self, module):
        # Import machinery gắn spec của tên short lên module backend.*; trả lại spec gốc
        # để relative import và importlib.reload vẫn thấy đúng package.
        if self._original_spec is not None:
            module.__spec__ = self._original_spec


class _BackendAliasFinder(importlib.abc.MetaPathFinder):
    """Một object duy nhất cho mỗi file backend, dù test import `x` hay `backend.x`.

    Hai bản module tách rời làm mock.patch("x...") và mock.patch("backend.x...")
    trỏ vào hai bản khác nhau, khiến patch rơi vào bản không được gọi.
    """

    _backend_alias_marker = True

    def find_spec(self, fullname, path=None, target=None):
        if fullname == "backend" or fullname.startswith("backend."):
            return None
        relative = fullname.replace(".", "/")
        if (backend_dir / (relative + ".py")).is_file() or (
            backend_dir / relative / "__init__.py"
        ).is_file():
            return importlib.util.spec_from_loader(
                fullname, _BackendAliasLoader("backend." + fullname)
            )
        return None


if not any(getattr(finder, "_backend_alias_marker", False) for finder in sys.meta_path):
    sys.meta_path.insert(0, _BackendAliasFinder())


@pytest.fixture(autouse=True)
def _isolate_shared_stop_state(tmp_path):
    """Keep stop flags and tracker files out of the live workspace while tests run."""
    import job_tracker
    import shared_state

    workspace = tmp_path / "_isolated_workspace"
    (workspace / "control").mkdir(parents=True)
    (workspace / "bot_system").mkdir(parents=True)

    # Một số test thay sys.modules bằng stub; khi đó không có gì để cách ly.
    old_paths = None
    if hasattr(job_tracker, "WORKSPACE"):
        old_paths = (job_tracker.WORKSPACE, job_tracker.STATUS_FILE, job_tracker.BATCH_LOCK_FILE)
        job_tracker.WORKSPACE = workspace
        job_tracker.STATUS_FILE = workspace / "bot_system" / "job_status.json"
        job_tracker.BATCH_LOCK_FILE = workspace / "bot_system" / "batch.lock"

    fget = getattr(getattr(type(shared_state), "stop_requested", None), "fget", None)
    old_local_stop = getattr(shared_state, "_local_stop", False)
    if hasattr(shared_state, "_local_stop"):
        shared_state._local_stop = False
    try:
        if fget is None:
            yield
        else:
            with patch.dict(fget.__globals__, {"WORKSPACE": workspace}):
                yield
    finally:
        if hasattr(shared_state, "_local_stop"):
            shared_state._local_stop = old_local_stop
        if old_paths is not None:
            job_tracker.WORKSPACE, job_tracker.STATUS_FILE, job_tracker.BATCH_LOCK_FILE = old_paths


def pytest_sessionfinish(session, exitstatus):
    shutil.rmtree(_TEST_WORKSPACE, ignore_errors=True)
