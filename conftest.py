import importlib
import importlib.abc
import importlib.util
import sys
from pathlib import Path

root = Path(__file__).resolve().parent
backend_dir = root / "backend"
if str(backend_dir) not in sys.path:
    sys.path.insert(0, str(backend_dir))
if str(root) not in sys.path:
    sys.path.insert(0, str(root))

import pytest
from unittest.mock import patch


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
    """Keep stop flags out of the live workspace while tests run."""
    import shared_state

    fget = getattr(getattr(type(shared_state), "stop_requested", None), "fget", None)
    if fget is None:
        yield
        return
    old_local_stop = getattr(shared_state, "_local_stop", False)
    shared_state._local_stop = False
    try:
        with patch.dict(fget.__globals__, {"WORKSPACE": tmp_path}):
            yield
    finally:
        shared_state._local_stop = old_local_stop
