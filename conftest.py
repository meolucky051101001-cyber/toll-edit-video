import sys
from pathlib import Path

root = Path(__file__).resolve().parent
backend_dir = root / "backend"
if str(backend_dir) not in sys.path:
    sys.path.insert(0, str(backend_dir))
if str(root) not in sys.path:
    sys.path.insert(0, str(root))

import pytest

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
