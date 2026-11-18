"""Tool V1 Backend Package."""
import sys
from pathlib import Path

_backend_dir = Path(__file__).resolve().parent
if str(_backend_dir) not in sys.path:
    sys.path.insert(0, str(_backend_dir))

def __getattr__(name: str):
    import importlib
    existing = sys.modules.get(f"backend.{name}") or sys.modules.get(name)
    if existing is not None:
        sys.modules.setdefault(name, existing)
        sys.modules.setdefault(f"backend.{name}", existing)
        setattr(sys.modules[__name__], name, existing)
        return existing
    try:
        mod = importlib.import_module(name)
        sys.modules.setdefault(f"backend.{name}", mod)
        setattr(sys.modules[__name__], name, mod)
        return mod
    except ImportError:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
