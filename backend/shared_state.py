"""
shared_state.py - Cross-process shared state synchronization for Antigravity Video Dubbing Tool.
Supports multi-process coordination between main.py, telegram_bot.py, and background workers.
"""
import sys
import os
import types
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
WORKSPACE = Path(os.getenv("AUTODUB_WORKSPACE", str(BASE_DIR.parent / "workspace")))
CONTROL_DIR = Path(os.getenv("AUTODUB_CONTROL_DIR", str(WORKSPACE / "control")))

class _SharedState(types.ModuleType):
    def __init__(self, name):
        super().__init__(name)
        self._local_stop = False

    @property
    def stop_requested(self) -> bool:
        if self._local_stop:
            return True
        try:
            if (CONTROL_DIR / "video.stop").exists():
                return True
        except Exception:
            pass
        try:
            if (WORKSPACE / "bot_system" / "stop_request.json").exists():
                return True
        except Exception:
            pass
        return False

    @stop_requested.setter
    def stop_requested(self, val: bool):
        self._local_stop = bool(val)
        if val:
            try:
                CONTROL_DIR.mkdir(parents=True, exist_ok=True)
                (CONTROL_DIR / "video.stop").write_text("1", encoding="utf-8")
            except Exception:
                pass
        else:
            try:
                (CONTROL_DIR / "video.stop").unlink(missing_ok=True)
            except Exception:
                pass
            try:
                (WORKSPACE / "bot_system" / "stop_request.json").unlink(missing_ok=True)
            except Exception:
                pass

sys.modules[__name__] = _SharedState(__name__)
