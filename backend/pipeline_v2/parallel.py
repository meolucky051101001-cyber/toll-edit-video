"""Controlled overlap of the one GPU OCR job and network translation."""

from __future__ import annotations

import asyncio
import inspect
from dataclasses import dataclass
from typing import Any, Callable


@dataclass(frozen=True)
class ParallelContextResult:
    ocr: Any
    translation: Any
    ran_in_parallel: bool


async def _invoke(callback: Callable[[], Any]) -> Any:
    if inspect.iscoroutinefunction(callback):
        return await callback()
    result = await asyncio.to_thread(callback)
    if inspect.isawaitable(result):
        return await result
    return result


async def run_ocr_and_translation(
    ocr_callback: Callable[[], Any],
    translation_callback: Callable[[], Any],
    enabled: bool,
) -> ParallelContextResult:
    if enabled:
        t_ocr = asyncio.create_task(_invoke(ocr_callback))
        t_trans = asyncio.create_task(_invoke(translation_callback))
        try:
            ocr_result, translation_result = await asyncio.gather(t_ocr, t_trans)
            return ParallelContextResult(ocr_result, translation_result, True)
        except BaseException:
            for task in (t_ocr, t_trans):
                if not task.done():
                    task.cancel()
            await asyncio.gather(t_ocr, t_trans, return_exceptions=True)
            raise
    ocr_result = await _invoke(ocr_callback)
    translation_result = await _invoke(translation_callback)
    return ParallelContextResult(ocr_result, translation_result, False)


