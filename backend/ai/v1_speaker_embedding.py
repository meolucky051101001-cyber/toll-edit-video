"""CAM++ 3D-Speaker ONNX embedding & vocal-tract acoustic gender helper for Tool V1.

Uses Alibaba 3D-Speaker CAM++ (`3dspeaker_speech_campplus_sv_zh-cn_16k-common.onnx`, ~27MB)
for 512-D L2-normalized speaker identity embeddings (speaker turn clustering & verification)
combined with vocal-tract spectral envelope analysis to disambiguate ambiguous pitch
regions (160-205 Hz alto-female vs high-male speakers).
"""

from __future__ import annotations

import logging
import math
import os
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

import numpy as np

from config.paths import repo_root

logger = logging.getLogger(__name__)

DEFAULT_CAMPLUS_PATH = (
    repo_root() / "models" / "v1" / "speaker_id" / "3dspeaker_speech_campplus_sv_zh-cn_16k-common.onnx"
)
_EXTRACTOR_CACHE: Dict[str, Any] = {}


def check_camplus_readiness(model_path: Path = DEFAULT_CAMPLUS_PATH) -> Tuple[bool, str]:
    import importlib.util
    import sys

    if ("sherpa_onnx" in sys.modules and sys.modules["sherpa_onnx"] is None) or (
        "sherpa_onnx" not in sys.modules and importlib.util.find_spec("sherpa_onnx") is None
    ):
        return False, "Thiếu sherpa-onnx: No module named 'sherpa_onnx'"
    if not Path(model_path).is_file():
        return False, f"Thiếu model CAM++ tại {model_path}"
    return True, "Sẵn sàng (CAM++ 3D-Speaker 27MB ONNX)"


def _get_extractor(model_path: Path = DEFAULT_CAMPLUS_PATH):
    import sherpa_onnx

    p = Path(model_path)
    if not p.is_file():
        return None
    key = str(p.resolve())
    ext = _EXTRACTOR_CACHE.get(key)
    if ext is not None:
        return ext
    cfg = sherpa_onnx.SpeakerEmbeddingExtractorConfig(
        model=str(p),
        num_threads=max(1, min(4, (os.cpu_count() or 4) // 2)),
        provider="cpu",
    )
    ext = sherpa_onnx.SpeakerEmbeddingExtractor(cfg)
    _EXTRACTOR_CACHE[key] = ext
    return ext


def extract_speaker_embedding(samples_16k: np.ndarray, sr: int = 16000) -> Optional[np.ndarray]:
    """Compute L2-normalized 512-D CAM++ speaker embedding for a 16kHz float32 waveform."""
    if samples_16k is None or len(samples_16k) < int(sr * 0.25):
        return None
    try:
        ext = _get_extractor()
        if ext is None:
            return None
        wav = np.asarray(samples_16k, dtype=np.float32)
        pk = float(np.max(np.abs(wav)))
        if 1e-4 < pk < 0.85:
            wav = (wav * (0.85 / pk)).astype(np.float32)
        stream = ext.create_stream()
        stream.accept_waveform(int(sr), wav)
        stream.input_finished()
        if not ext.is_ready(stream):
            return None
        emb = np.asarray(ext.compute(stream), dtype=np.float32)
        norm = float(np.linalg.norm(emb))
        if norm <= 1e-8 or not math.isfinite(norm):
            return None
        return emb / norm
    except Exception as exc:
        logger.debug("[CAM++] Speaker embedding skipped: %s", exc)
        return None


def cosine_similarity(vec_a: np.ndarray, vec_b: np.ndarray) -> float:
    """Compute cosine similarity between two speaker embedding vectors."""
    a = np.asarray(vec_a, dtype=np.float32).ravel()
    b = np.asarray(vec_b, dtype=np.float32).ravel()
    if a.size == 0 or a.size != b.size:
        return 0.0
    denom = float(np.linalg.norm(a) * np.linalg.norm(b))
    if denom <= 1e-8 or not math.isfinite(denom):
        return 0.0
    return float(np.dot(a, b) / denom)


def refine_ambiguous_pitch_gender(
    samples_16k: np.ndarray,
    sr: int,
    median_f0: float,
    base_gender: str,
    base_confidence: float,
) -> Tuple[str, float]:
    """Disambiguate borderline F0 (160-205 Hz) using vocal-tract spectral formant balance."""
    if samples_16k is None or len(samples_16k) < int(sr * 0.25):
        return base_gender, base_confidence
    if not (158.0 <= float(median_f0) <= 208.0):
        return base_gender, base_confidence
    try:
        wav = np.asarray(samples_16k, dtype=np.float32)
        win_len = 512
        hop = 256
        if len(wav) < win_len:
            return base_gender, base_confidence
        n_frames = 1 + (len(wav) - win_len) // hop
        idx = np.arange(win_len)[None, :] + (np.arange(n_frames) * hop)[:, None]
        frames = wav[idx] * np.hanning(win_len)[None, :]
        frame_rms = np.sqrt(np.mean(frames * frames, axis=1))
        voiced = frames[frame_rms > np.percentile(frame_rms, 45)]
        if len(voiced) < 4:
            return base_gender, base_confidence

        spec = np.mean(np.abs(np.fft.rfft(voiced, n=512)) ** 2, axis=0)
        freqs = np.fft.rfftfreq(512, d=1.0 / float(sr))
        # Male vocal tracts (~17cm) concentrate chest resonance in 280-850 Hz;
        # female vocal tracts (~14cm) shift F1/F2/F3 energy upward into 1150-3600 Hz.
        low_chest = float(np.sum(spec[(freqs >= 280.0) & (freqs < 850.0)])) + 1e-9
        upper_formant = float(np.sum(spec[(freqs >= 1150.0) & (freqs <= 3600.0)])) + 1e-9
        formant_ratio_db = 10.0 * math.log10(upper_formant / low_chest)

        # Combine pitch z-score around 180 Hz with vocal-tract formant shift
        f0_score = (float(median_f0) - 180.0) / 16.0  # >0 female-leaning, <0 male-leaning
        vt_score = (formant_ratio_db + 7.5) / 4.5      # >0 female-leaning, <0 male-leaning
        fused = 0.62 * f0_score + 0.38 * max(-1.8, min(1.8, vt_score))

        # Verify speaker signal stability via CAM++ embedding norm/validity
        has_emb = extract_speaker_embedding(wav, sr=sr) is not None
        boost = 0.05 if has_emb else 0.0

        if fused >= 0.12:
            conf = min(0.96, max(base_confidence if base_gender == "female" else 0.72, 0.72 + abs(fused) * 0.15 + boost))
            return "female", round(conf, 3)
        if fused <= -0.12:
            conf = min(0.96, max(base_confidence if base_gender == "male" else 0.72, 0.72 + abs(fused) * 0.15 + boost))
            return "male", round(conf, 3)
        return base_gender, base_confidence
    except Exception as exc:
        logger.debug("[CAM++] Formant gender refinement skipped: %s", exc)
        return base_gender, base_confidence
