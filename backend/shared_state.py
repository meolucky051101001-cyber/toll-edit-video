"""
shared_state.py - Cross-process shared state synchronization for Antigravity Video Dubbing Tool.
Supports multi-process coordination between main.py, telegram_bot.py, and background workers.
"""
import sys
import os
import types
from pathlib import Path

import stop_service

BASE_DIR = Path(__file__).resolve().parent
WORKSPACE = Path(os.getenv("AUTODUB_WORKSPACE", str(BASE_DIR.parent / "workspace")))

class _SharedState(types.ModuleType):
    def __init__(self, name):
        super().__init__(name)
        self._local_stop = False

    @property
    def stop_requested(self) -> bool:
        if os.getenv('AUTODUB_DISABLE_STOP_CHECK', '0').lower() in ('1', 'true', 'yes'):
            return False
        if self._local_stop:
            return True
        try:
            return stop_service.is_stop_requested(WORKSPACE)
        except Exception:
            return False

    @stop_requested.setter
    def stop_requested(self, val: bool):
        self._local_stop = bool(val)
        try:
            if val:
                stop_service.request_stop(WORKSPACE, source="shared_state", reason="stop_requested=True")
            elif stop_service.is_stop_requested(WORKSPACE):
                stop_service.clear_stop(WORKSPACE, source="shared_state", reason="stop_requested=False")
        except Exception:
            pass

sys.modules[__name__] = _SharedState(__name__)
