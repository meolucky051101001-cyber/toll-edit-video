from __future__ import annotations

import logging
from typing import Any, Callable

logger = logging.getLogger(__name__)


def run_with_encoder_fallback(
    build_cmd: Callable[[list[str]], list[str]],
    nvenc_args: list[str],
    x264_args: list[str],
    *,
    run: Callable[[list[str]], Any],
    ok: Callable[[Any], bool],
    before_fallback: Callable[[], None] | None = None,
    skip_nvenc: bool = False,
    should_stop: Callable[[], bool] | None = None,
) -> tuple[bool, str]:
    if should_stop and should_stop():
        return False, ""

    if not skip_nvenc:
        try:
            result = run(build_cmd(nvenc_args))
            if ok(result):
                return True, "h264_nvenc"
        except Exception:
            logger.warning("NVENC encoding failed; falling back to libx264", exc_info=True)

    if should_stop and should_stop():
        return False, ""
    if before_fallback:
        before_fallback()

    result = run(build_cmd(x264_args))
    if ok(result):
        return True, "libx264"
    return False, ""
