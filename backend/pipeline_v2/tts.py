"""Pipeline-v2 TTS generation without in-process RVC model loading."""

from __future__ import annotations

import asyncio
import logging
import os
import sys
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional

logger = logging.getLogger(__name__)

from .timing import TimingPolicy, fit_audio_to_window

try:
    from ..subtitle_text import normalize_subtitle_text
except ImportError:
    from subtitle_text import normalize_subtitle_text


def _prepare_legacy_imports() -> None:
    backend_directory = Path(__file__).resolve().parents[1]
    if str(backend_directory) not in sys.path:
        sys.path.insert(0, str(backend_directory))
    import ai
    if "ai.voice_cloning" in sys.modules:
        setattr(ai, "voice_cloning", sys.modules["ai.voice_cloning"])


def resolve_mapped_voice(voice_source, speaker_voice_map, speaker_id, gender,
                         enable_auto_gender=False):
    """Resolve an explicit mapping from speaker_voice_map across providers.

    Fixed RVC keeps its configured base voice unless auto gender / dual voice is explicitly enabled.
    """
    if not speaker_voice_map or (voice_source == "rvc" and not enable_auto_gender):
        return None
    candidate = speaker_voice_map.get(speaker_id, speaker_voice_map.get(gender))
    voice = str(candidate or "").strip()
    return voice or None


async def generate_tts_audio_v2(
    segments: Iterable[Any],
    output_directory: os.PathLike,
    voice_source: str = "edge",
    voice_param: str = "vi-VN-HoaiMyNeural",
    api_key: str = "",
    policy: TimingPolicy = None,
    strict_provider: bool = False,
    enable_auto_gender: bool = False,
    speaker_voice_map: Optional[Mapping[str, str]] = None,
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
    semaphore = asyncio.Semaphore(int(os.getenv("TTS_CONCURRENCY", "3")))

    async def one(segment: Any) -> Dict[str, Any]:
        async with semaphore:
            raw = output / "{}_raw.mp3".format(segment.index)
            fitted = output / "{}.mp3".format(segment.index)
            text = normalize_subtitle_text(segment.content)
            segment.content = text
            seg_gender = str(getattr(segment, "gender", "female") or "female").lower()
            speaker_id = str(getattr(segment, "speaker_id", "") or "").strip()
            mapped_voice = resolve_mapped_voice(
                voice_source, speaker_voice_map, speaker_id, seg_gender, enable_auto_gender
            )

            target = max((segment.end - segment.start).total_seconds(), 0.1)

            async def _synthesize_edge_with_rescue(
                edge_voice: str,
                edge_pitch: str = "+0Hz",
                edge_rate: str = "+0%",
                allow_capcut_rescue: bool = False,
            ) -> bool:
                is_silent = False
                try:
                    is_silent = bool(await generate_tts_edge(
                        text, str(raw), edge_voice, pitch=edge_pitch, rate=edge_rate, target_duration=target
                    ))
                except Exception as edge_err:
                    logger.warning("Edge TTS exception for %r: %s", text, edge_err)
                    is_silent = True

                if is_silent and allow_capcut_rescue and not strict_provider:
                    try:
                        capcut_voice = "BV075_streaming" if (enable_auto_gender and seg_gender == "male") else "BV562_streaming"
                        await asyncio.to_thread(_run_capcut_tts, text, str(raw), capcut_voice)
                        if raw.is_file() and raw.stat().st_size > 256:
                            is_silent = False
                    except Exception as cap_err:
                        logger.warning("CapCut rescue also failed for %r: %s", text, cap_err)
                return is_silent

            clean_chars = [c for c in text if c.isalnum() or '\u4e00' <= c <= '\u9fff']
            is_silent_fallback = False
            if bool(getattr(segment, "is_unvoiced_scene", False)) or not clean_chars:
                await generate_tts_edge("", str(raw), target_duration=target)
                is_silent_fallback = False
            elif mapped_voice:
                if mapped_voice.startswith("vi-"):
                    pitch = "+15Hz" if mapped_voice == "vi-VN-HoaiMyNeural" else "+0Hz"
                    rate = "+15%" if mapped_voice == "vi-VN-HoaiMyNeural" else "+5%"
                    is_silent_fallback = await _synthesize_edge_with_rescue(
                        mapped_voice, edge_pitch=pitch, edge_rate=rate, allow_capcut_rescue=False
                    )
                elif mapped_voice in {"banmai", "leminh", "myan", "thuminh", "giahuy"}:
                    await generate_tts_fpt(text, str(raw), api_key, voice=mapped_voice)
                elif mapped_voice.startswith(("BV", "multi_", "vi_")) or voice_source == "capcut":
                    await asyncio.to_thread(_run_capcut_tts, text, str(raw), mapped_voice)
                elif voice_source == "fpt":
                    await generate_tts_fpt(text, str(raw), api_key, voice=mapped_voice)
                elif voice_source == "edge":
                    is_silent_fallback = await _synthesize_edge_with_rescue(
                        mapped_voice, allow_capcut_rescue=False
                    )
                elif voice_source == "vieneu" or mapped_voice.startswith("vieneu-"):
                    from ai.vieneu_tts_service import generate_tts_vieneu
                    await generate_tts_vieneu(text, str(raw), voice=mapped_voice)
                else:
                    await asyncio.to_thread(_run_capcut_tts, text, str(raw), mapped_voice)
            elif voice_source == "vieneu":
                from ai.vieneu_tts_service import generate_tts_vieneu
                v_target = voice_param or "Hải Đăng"
                if enable_auto_gender:
                    if seg_gender == "male":
                        v_target = "Hải Đăng"
                    elif seg_gender == "female":
                        v_target = "Mai Anh"
                await generate_tts_vieneu(text, str(raw), voice=v_target)
            elif voice_source == "capcut":
                capcut_v = "BV075_streaming" if (enable_auto_gender and seg_gender == "male") else voice_param
                await asyncio.to_thread(_run_capcut_tts, text, str(raw), capcut_v)
            elif voice_source == "edge":
                edge_v = (
                    "vi-VN-NamMinhNeural"
                    if (enable_auto_gender and seg_gender == "male")
                    else (voice_param if voice_param.startswith("vi-") else "vi-VN-HoaiMyNeural")
                )
                pitch = "+15Hz" if edge_v == "vi-VN-HoaiMyNeural" else "+0Hz"
                rate = "+15%" if edge_v == "vi-VN-HoaiMyNeural" else "+5%"
                is_silent_fallback = await _synthesize_edge_with_rescue(
                    edge_v, edge_pitch=pitch, edge_rate=rate, allow_capcut_rescue=False
                )
            elif voice_source == "fpt":
                try:
                    fpt_v = (
                        "leminh"
                        if (enable_auto_gender and seg_gender == "male")
                        else (voice_param if not voice_param.startswith("vi-") else "banmai")
                    )
                    await generate_tts_fpt(text, str(raw), api_key, voice=fpt_v)
                except FPTQuotaError as exc:
                    if strict_provider:
                        raise RuntimeError(
                            "FPT TTS is unavailable; refusing silent provider fallback"
                        ) from exc
                    is_silent_fallback = await _synthesize_edge_with_rescue(
                        "vi-VN-HoaiMyNeural", edge_rate="+5%", allow_capcut_rescue=False
                    )
            elif voice_source == "rvc":
                rvc_voice = "BV075_streaming" if (enable_auto_gender and seg_gender == "male") else "BV562_streaming"
                try:
                    await asyncio.to_thread(
                        _run_capcut_tts, text, str(raw), rvc_voice
                    )
                except Exception as cap_err:
                    logger.warning("CapCut base TTS failed for %r: %s -> using Edge TTS as vocal guide for RVC", text, cap_err)
                    is_silent_fallback = await _synthesize_edge_with_rescue(
                        "vi-VN-HoaiMyNeural", edge_rate="+0%", allow_capcut_rescue=False
                    )
            else:
                pitch = "+15Hz" if voice_param == "vi-VN-HoaiMyNeural" else "+0Hz"
                rate = "+15%" if voice_param == "vi-VN-HoaiMyNeural" else "+5%"
                is_silent_fallback = await _synthesize_edge_with_rescue(
                    voice_param, edge_pitch=pitch, edge_rate=rate
                )
            if not raw.is_file() or raw.stat().st_size == 0:
                is_silent_fallback = True
                silence_dur = f"{max(0.2, float(target)):.2f}"
                import subprocess
                subprocess.run([
                    "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
                    "-f", "lavfi", "-i", "anullsrc=r=24000:cl=mono",
                    "-t", silence_dur, "-c:a", "libmp3lame", "-b:a", "64k", str(raw)
                ], check=False)
            elif raw.stat().st_size > 1024 and not bool(getattr(segment, "is_unvoiced_scene", False)):
                is_silent_fallback = False
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
                "is_silent_fallback": is_silent_fallback,
                "is_unvoiced_scene": bool(getattr(segment, "is_unvoiced_scene", False)),
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
