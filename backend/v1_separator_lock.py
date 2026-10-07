"""
v1_separator_lock.py - Cross-process locking mechanism for heavy audio separation on Tool V1.
Ensures strictly 1 heavy separation model (BS-RoFormer or Demucs) runs on the RTX 4050 GPU at a time.
"""

import os
import sys
import time
import logging
from contextlib import contextmanager
from pathlib import Path

logger = logging.getLogger("v1_separator_lock")

ROOT_DIR = Path(__file__).resolve().parent
WORKSPACE = Path(os.getenv("AUTODUB_WORKSPACE", str(ROOT_DIR.parent / "workspace")))
LOCK_FILE = WORKSPACE / "bot_system" / "separator.lock"


@contextmanager
def separator_gpu_lock(timeout_seconds: float = 300.0, poll_interval: float = 0.5):
    """
    Acquire cross-process exclusive lock for GPU audio separation via unified GPU Gatekeeper.
    Releases automatically on context exit or process termination.
    """
    from v1_gpu_gatekeeper import gpu_gatekeeper
    with gpu_gatekeeper(stage_name="vocal_separation", timeout_seconds=timeout_seconds, poll_interval=poll_interval):
        yield

