"""Fast Silero VAD + SenseVoice acoustic speech-presence gate for Tool V1.

Detects speechless BGM / ASMR clips in <1s before running heavyweight GPU
vocal separation (BS-RoFormer) and speech ASR. Peak-normalizes quiet audio and
cross-checks with SenseVoice CTC event tags so whispered ASMR speech is NEVER
falsely skipped, failing open (`has_speech=True`) on any uncertainty.
"""

from __future__ import annotations

import logging
import math
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Dict, List

import numpy as np

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class VADGateResult:
    has_speech: bool
    speech_seconds: float
    total_seconds: float
    speech_ratio: float
    segment_count: int
    rms_dbfs: float
    elapsed_s: float
    reason: str

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def _load_mono_16k_unit(audio_path: str | Path) -> tuple[np.ndarray, float]:
    import soundfile as sf

    data, sr = sf.read(str(audio_path), dtype="float32", always_2d=True)
    if data.size == 0 or sr <= 0:
        return np.zeros(0, dtype=np.float32), 0.0
    mono = data.mean(axis=1) if data.shape[1] > 1 else data[:, 0]
    dur = float(len(mono)) / float(sr)
    if int(sr) != 16000:
        try:
            from scipy.signal import resample_poly

            g = math.gcd(int(sr), 16000)
            mono = resample_poly(mono, 16000 // g, int(sr) // g).astype(np.float32)
        except ImportError:
            n_out = max(1, int(round(len(mono) * 16000.0 / float(sr))))
            mono = np.interp(
                np.linspace(0.0, len(mono) - 1, n_out),
                np.arange(len(mono), dtype=np.float64),
                mono,
            ).astype(np.float32)
    else:
        mono = np.asarray(mono, dtype=np.float32)
    return mono, dur


def analyze_speech_presence_vad(
    audio_path: str | Path,
    *,
    min_speech_seconds: float = 0.60,
    min_speech_ratio: float = 0.02,
    threshold: float = 0.25,
) -> VADGateResult:
    """Return whether `audio_path` contains human speech worth separating/transcribing."""
    t0 = time.monotonic()
    p = Path(audio_path)
    if not p.is_file():
        return VADGateResult(
            has_speech=True,
            speech_seconds=0.0,
            total_seconds=0.0,
            speech_ratio=1.0,
            segment_count=0,
            rms_dbfs=0.0,
            elapsed_s=round(time.monotonic() - t0, 4),
            reason="missing_file_fail_safe",
        )
    try:
        mono, total_s = _load_mono_16k_unit(p)
        if total_s <= 0.1 or mono.size < 1600:
            return VADGateResult(
                has_speech=False,
                speech_seconds=0.0,
                total_seconds=round(total_s, 3),
                speech_ratio=0.0,
                segment_count=0,
                rms_dbfs=-120.0,
                elapsed_s=round(time.monotonic() - t0, 4),
                reason="too_short_or_empty",
            )

        energy = float(np.mean(mono * mono))
        rms_dbfs = 20.0 * math.log10(max(1e-9, math.sqrt(max(0.0, energy))))
        if rms_dbfs < -68.0:
            return VADGateResult(
                has_speech=False,
                speech_seconds=0.0,
                total_seconds=round(total_s, 3),
                speech_ratio=0.0,
                segment_count=0,
                rms_dbfs=round(rms_dbfs, 2),
                elapsed_s=round(time.monotonic() - t0, 4),
                reason="digital_silence",
            )

        peak = float(np.max(np.abs(mono)))
        norm_mono = (mono * (0.85 / peak)).astype(np.float32) if 1e-4 < peak < 0.85 else mono

        from faster_whisper.vad import VadOptions, get_speech_timestamps

        opts = VadOptions(
            threshold=float(threshold),
            min_speech_duration_ms=200,
            min_silence_duration_ms=300,
            speech_pad_ms=80,
        )
        spans: List[dict] = get_speech_timestamps(norm_mono, vad_options=opts, sampling_rate=16000)
        speech_samples = sum(max(0, int(sp["end"]) - int(sp["start"])) for sp in spans)
        speech_s = float(speech_samples) / 16000.0
        ratio = speech_s / max(total_s, 0.1)

        vad_says_speech = not (
            speech_s < float(min_speech_seconds)
            and ratio < float(min_speech_ratio)
            and len(spans) <= 1
        )
        if vad_says_speech:
            elapsed = time.monotonic() - t0
            logger.info(
                "[VAD_GATE] %s: speech=%.2fs/%.1fs (%.1f%%, spans=%d) -> has_speech=True in %.3fs",
                p.name,
                speech_s,
                total_s,
                ratio * 100.0,
                len(spans),
                elapsed,
            )
            return VADGateResult(
                has_speech=True,
                speech_seconds=round(speech_s, 3),
                total_seconds=round(total_s, 3),
                speech_ratio=round(ratio, 4),
                segment_count=len(spans),
                rms_dbfs=round(rms_dbfs, 2),
                elapsed_s=round(elapsed, 4),
                reason="speech_detected_vad",
            )

        # Cross-check whispered / ASMR speech with SenseVoice CTC before bypassing separation.
        try:
            try:
                from .v1_sensevoice_adapter import analyze_audio_sensevoice, check_sensevoice_readiness
            except ImportError:
                from ai.v1_sensevoice_adapter import analyze_audio_sensevoice, check_sensevoice_readiness
            sv_ready, _ = check_sensevoice_readiness(ignore_flag=True)
            if sv_ready:
                sv_res = analyze_audio_sensevoice(p)
                sv_tokens = sv_res.get("tokens") or []
                if len(sv_tokens) >= 4:
                    sv_dur = sum(max(0.0, float(t["end"]) - float(t["start"])) for t in sv_tokens)
                    elapsed = time.monotonic() - t0
                    logger.info(
                        "[VAD_GATE] %s: Silero low but SenseVoice found %d speech tokens (%.2fs) -> has_speech=True in %.3fs",
                        p.name,
                        len(sv_tokens),
                        sv_dur,
                        elapsed,
                    )
                    return VADGateResult(
                        has_speech=True,
                        speech_seconds=round(sv_dur, 3),
                        total_seconds=round(total_s, 3),
                        speech_ratio=round(sv_dur / max(total_s, 0.1), 4),
                        segment_count=max(1, len(sv_tokens) // 4),
                        rms_dbfs=round(rms_dbfs, 2),
                        elapsed_s=round(elapsed, 4),
                        reason="whispered_speech_rescued_by_sensevoice",
                    )
        except Exception as sv_exc:
            logger.debug("[VAD_GATE] SenseVoice cross-check skipped: %s", sv_exc)

        elapsed = time.monotonic() - t0
        logger.info(
            "[VAD_GATE] %s: speech=%.2fs/%.1fs (%.1f%%) -> has_speech=False (BGM/ASMR only) in %.3fs",
            p.name,
            speech_s,
            total_s,
            ratio * 100.0,
            elapsed,
        )
        return VADGateResult(
            has_speech=False,
            speech_seconds=round(speech_s, 3),
            total_seconds=round(total_s, 3),
            speech_ratio=round(ratio, 4),
            segment_count=len(spans),
            rms_dbfs=round(rms_dbfs, 2),
            elapsed_s=round(elapsed, 4),
            reason="bgm_or_asmr_only",
        )
    except Exception as exc:
        logger.warning("[VAD_GATE] Fallback has_speech=True on error: %s", exc)
        return VADGateResult(
            has_speech=True,
            speech_seconds=0.0,
            total_seconds=0.0,
            speech_ratio=1.0,
            segment_count=0,
            rms_dbfs=0.0,
            elapsed_s=round(time.monotonic() - t0, 4),
            reason=f"error_fail_safe:{type(exc).__name__}",
        )
