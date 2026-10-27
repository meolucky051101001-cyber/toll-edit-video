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

def sync_backend_modules():
    for k in list(sys.modules.keys()):
        if k.startswith("backend."):
            short = k[len("backend."):]
            if short not in sys.modules or sys.modules[short] is not sys.modules[k]:
                sys.modules[short] = sys.modules[k]
        elif "." not in k and (backend_dir / f"{k}.py").is_file():
            full = f"backend.{k}"
            if full not in sys.modules:
                sys.modules[full] = sys.modules[k]
            elif sys.modules[full] is not sys.modules[k]:
                sys.modules[full] = sys.modules[k]

@pytest.fixture(autouse=True)
def _auto_sync_modules():
    sync_backend_modules()
    yield
    sync_backend_modules()


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
