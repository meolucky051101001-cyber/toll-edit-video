"""Pipeline-v2 TTS generation without in-process RVC model loading."""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path
from typing import Any, Dict, Iterable, List

from .timing import TimingPolicy, fit_audio_to_window


def _prepare_legacy_imports() -> None:
    backend_directory = Path(__file__).resolve().parents[1]
    if str(backend_directory) not in sys.path:
        sys.path.insert(0, str(backend_directory))


async def generate_tts_audio_v2(
    segments: Iterable[Any],
    output_directory: os.PathLike,
    voice_source: str = "edge",
    voice_param: str = "vi-VN-HoaiMyNeural",
    api_key: str = "",
    policy: TimingPolicy = None,
    strict_provider: bool = False,
) -> List[Dict[str, Any]]:
    """Generate TTS and apply at most the configured light atempo correction."""

    _prepare_legacy_imports()
    from ai.voice_cloning import (
        FPTQuotaError,
        _run_capcut_tts,
        generate_tts_edge,
        generate_tts_fpt,
    )

    config = policy or TimingPolicy()
    output = Path(output_directory)
    output.mkdir(parents=True, exist_ok=True)
    semaphore = asyncio.Semaphore(3)

    async def one(segment: Any) -> Dict[str, Any]:
        async with semaphore:
            raw = output / "{}_raw.mp3".format(segment.index)
            fitted = output / "{}.mp3".format(segment.index)
            text = str(segment.content).strip()
            seg_gender = str(getattr(segment, "gender", "female") or "female").lower()
            if seg_gender == "male":
                try:
                    await asyncio.to_thread(
                        _run_capcut_tts, text, str(raw), "BV075_streaming"
                    )
                except Exception:
                    await generate_tts_edge(
                        text, str(raw), "vi-VN-NamMinhNeural", rate="+5%", pitch="+0Hz"
                    )
            elif voice_source == "fpt":
                try:
                    await generate_tts_fpt(text, str(raw), api_key, voice="banmai")
                except FPTQuotaError as exc:
                    if strict_provider:
                        raise RuntimeError(
                            "FPT TTS is unavailable; refusing silent provider fallback"
                        ) from exc
                    await generate_tts_edge(
                        text, str(raw), "vi-VN-HoaiMyNeural", rate="+5%"
                    )
            elif voice_source == "rvc":
                try:
                    await asyncio.to_thread(
                        _run_capcut_tts, text, str(raw), "BV562_streaming"
                    )
                except Exception:
                    await generate_tts_edge(
                        text, str(raw), "vi-VN-HoaiMyNeural", rate="+0%"
                    )
            else:
                await generate_tts_edge(
                    text,
                    str(raw),
                    voice_param,
                    pitch="+15Hz" if voice_param == "vi-VN-HoaiMyNeural" else "+0Hz",
                    rate="+15%" if voice_param == "vi-VN-HoaiMyNeural" else "+5%",
                )
            target = max((segment.end - segment.start).total_seconds(), 0.1)
            fit = await asyncio.to_thread(
                fit_audio_to_window, raw, fitted, target, config
            )
            try:
                raw.unlink()
            except FileNotFoundError:
                pass
            return {
                "index": int(segment.index),
                "source_segment_id": int(
                    getattr(segment, "source_segment_id", None) or segment.index
                ),
                "path": str(fitted),
                "start": segment.start.total_seconds(),
                "end": segment.end.total_seconds(),
                "source_audio_duration": fit.source_duration_seconds,
                "target_audio_duration": fit.target_duration_seconds,
                "actual_audio_duration": fit.output_duration_seconds,
                "applied_atempo": fit.applied_atempo,
                "timing_fits": fit.fits,
                "content": text,
                "gender": seg_gender,
            }

    tasks = [asyncio.create_task(one(segment)) for segment in segments]
    try:
        return list(await asyncio.gather(*tasks))
    except BaseException:
        # asyncio.gather does not reliably cancel sibling work when one item
        # fails. Drain explicit cancellations before the caller removes the
        # batch temporary directory.
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        raise
