"""Paraformer-large-zh + CT-Transformer Punctuation adapter for Tool V1.

Provides:
1. Ultra-fast Chinese/Chinese-English transcription (`paraformer_zh` mode, ~3-5s per 60s clip)
   paired with SenseVoice CTC 60ms boundary alignment and BGM/non-speech event filtering,
   with automatic fallback signal when non-Chinese speech is detected.
2. Co-recognition enrichment (`enrich_segments_with_paraformer`) for `whisper_sensevoice`
   mode: attaches `paraformer_alt` for Stage 5 Gemini homophone disambiguation and rescues
   quiet whispered Chinese sentences missed by Whisper.
"""

from __future__ import annotations

import logging
import math
import os
import re
import time
from datetime import timedelta
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

import numpy as np
import srt

from config.paths import repo_root

logger = logging.getLogger(__name__)

DEFAULT_PARAFORMER_DIR = repo_root() / "models" / "v1" / "paraformer_zh"
DEFAULT_CT_PUNCT_DIR = repo_root() / "models" / "v1" / "ct_punct_zh_en"

_RECOGNIZER_CACHE: Dict[str, Any] = {}
_PUNCT_CACHE: Dict[str, Any] = {}


class NonChineseAudioError(RuntimeError):
    """Raised when audio is predominantly non-Chinese so caller falls back to Whisper."""


def _cjk_chars(text: str) -> str:
    return "".join(c for c in str(text or "") if ("\u3400" <= c <= "\u4dbf") or ("\u4e00" <= c <= "\u9fff"))


_CJK_SPACE_RE = re.compile(
    r"(?<=[\u3400-\u4dbf\u4e00-\u9fff])\s+(?=[\u3400-\u4dbf\u4e00-\u9fff0-9A-Za-z])"
    r"|(?<=[0-9A-Za-z])\s+(?=[\u3400-\u4dbf\u4e00-\u9fff])"
)


def _clean_paraformer_cjk_spacing(text: str) -> str:
    s = str(text or "").strip()
    if not s:
        return ""
    prev = None
    while prev != s:
        prev = s
        s = _CJK_SPACE_RE.sub("", s)
    return s


def check_paraformer_readiness() -> Tuple[bool, str]:
    try:
        import sherpa_onnx  # noqa: F401
    except ImportError as exc:
        return False, f"Thiếu thư viện sherpa-onnx: {exc}"

    model_file = DEFAULT_PARAFORMER_DIR / "model.int8.onnx"
    if not model_file.is_file():
        model_file = DEFAULT_PARAFORMER_DIR / "model.onnx"
    tokens_file = DEFAULT_PARAFORMER_DIR / "tokens.txt"
    if not model_file.is_file() or model_file.stat().st_size < 1_000_000:
        return False, f"Thiếu trọng số Paraformer ONNX tại {DEFAULT_PARAFORMER_DIR}"
    if not tokens_file.is_file():
        return False, f"Thiếu tokens.txt tại {DEFAULT_PARAFORMER_DIR}"

    punct_file = DEFAULT_CT_PUNCT_DIR / "model.int8.onnx"
    punct_note = " + CT-Transformer Punct" if punct_file.is_file() else ""
    return True, f"Sẵn sàng ({model_file.name}{punct_note})"


def _get_recognizer(model_dir: Path = DEFAULT_PARAFORMER_DIR):
    import sherpa_onnx

    key = str(Path(model_dir).resolve())
    rec = _RECOGNIZER_CACHE.get(key)
    if rec is not None:
        return rec
    model_file = model_dir / "model.int8.onnx"
    if not model_file.is_file():
        model_file = model_dir / "model.onnx"
    tokens_file = model_dir / "tokens.txt"
    threads = max(2, min(6, os.cpu_count() or 4))
    rec = sherpa_onnx.OfflineRecognizer.from_paraformer(
        paraformer=str(model_file),
        tokens=str(tokens_file),
        num_threads=threads,
        sample_rate=16000,
        feature_dim=80,
        decoding_method="greedy_search",
        provider="cpu",
    )
    _RECOGNIZER_CACHE[key] = rec
    return rec


def _get_punctuator(punct_dir: Path = DEFAULT_CT_PUNCT_DIR):
    import sherpa_onnx

    punct_file = punct_dir / "model.int8.onnx"
    if not punct_file.is_file():
        punct_file = punct_dir / "model.onnx"
    if not punct_file.is_file():
        return None
    key = str(punct_file.resolve())
    p = _PUNCT_CACHE.get(key)
    if p is not None:
        return p
    cfg = sherpa_onnx.OfflinePunctuationConfig(
        model=sherpa_onnx.OfflinePunctuationModelConfig(
            ct_transformer=str(punct_file),
            num_threads=max(2, min(4, (os.cpu_count() or 4) // 2)),
            provider="cpu",
        )
    )
    p = sherpa_onnx.OfflinePunctuation(cfg)
    _PUNCT_CACHE[key] = p
    return p


def _load_mono_16k(audio_path: str | Path) -> np.ndarray:
    import soundfile as sf

    data, sr = sf.read(str(audio_path), dtype="float32", always_2d=True)
    if data.size == 0 or sr <= 0:
        return np.zeros(0, dtype=np.float32)
    mono = data.mean(axis=1) if data.shape[1] > 1 else data[:, 0]
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
    return np.asarray(mono, dtype=np.float32)


def _decode_spans_paraformer(mono_16k: np.ndarray, spans: Sequence[Tuple[float, float]]) -> List[str]:
    if not spans or mono_16k.size == 0:
        return ["" for _ in spans]
    rec = _get_recognizer()
    punct = _get_punctuator()
    streams = []
    n_total = len(mono_16k)
    for start_s, end_s in spans:
        s0 = max(0, int(round((float(start_s) - 0.10) * 16000)))
        s1 = min(n_total, int(round((float(end_s) + 0.14) * 16000)))
        clip = mono_16k[s0:s1]
        if clip.size >= 800:
            pk = float(np.max(np.abs(clip)))
            if 1e-4 < pk < 0.85:
                clip = (clip * (0.85 / pk)).astype(np.float32)
        st = rec.create_stream()
        if clip.size > 0:
            st.accept_waveform(16000, clip)
        streams.append(st)
    rec.decode_streams(streams)
    results: List[str] = []
    for st in streams:
        raw = _clean_paraformer_cjk_spacing(str(getattr(st.result, "text", "") or ""))
        if raw and punct is not None:
            try:
                raw = _clean_paraformer_cjk_spacing(punct.add_punctuation(raw))
            except Exception:
                pass
        results.append(raw)
    return results


def transcribe_audio_paraformer(
    audio_path: str | Path,
    output_srt_path: Optional[str | Path] = None,
    *,
    original_audio_path: Optional[str | Path] = None,
) -> List[srt.Subtitle]:
    """Fast Chinese/Bilingual ASR combining SenseVoice CTC 60ms timing + Paraformer-large-zh."""
    try:
        from .v1_sensevoice_adapter import (
            analyze_audio_sensevoice,
            group_sensevoice_tokens_to_segments,
        )
    except ImportError:
        from ai.v1_sensevoice_adapter import (
            analyze_audio_sensevoice,
            group_sensevoice_tokens_to_segments,
        )

    t0 = time.monotonic()
    sv_data = analyze_audio_sensevoice(audio_path)
    windows = sv_data.get("windows") or []
    tokens = sv_data.get("tokens") or []
    if not tokens:
        return []

    non_zh_windows = sum(
        1
        for w in windows
        if str(w.get("lang_tag", "")) in ("<|en|>", "<|ja|>", "<|ko|>", "<|vi|>")
    )
    if windows and non_zh_windows > len(windows) * 0.5:
        raise NonChineseAudioError(
            f"Detected non-Chinese speech ({non_zh_windows}/{len(windows)} windows); routing to Whisper."
        )

    sv_segs = group_sensevoice_tokens_to_segments(tokens)
    if not sv_segs:
        return []

    mono = _load_mono_16k(audio_path)
    spans = [(float(s["start"]), float(s["end"])) for s in sv_segs]
    pf_texts = _decode_spans_paraformer(mono, spans)

    total_cjk = sum(len(_cjk_chars(t)) + len(_cjk_chars(s["text"])) for s, t in zip(sv_segs, pf_texts))
    if total_cjk == 0 and len(sv_segs) >= 2:
        raise NonChineseAudioError("No Chinese characters recognized; routing to Whisper.")

    subs: List[srt.Subtitle] = []
    for idx, (seg, pf_txt) in enumerate(zip(sv_segs, pf_texts), 1):
        sv_txt = str(seg.get("text", "") or "").strip()
        sv_conf = float(seg.get("confidence", 0.85) or 0.85)
        sv_cjk = _cjk_chars(sv_txt)
        pf_cjk = _cjk_chars(pf_txt)

        primary = sv_txt
        alt = pf_txt
        if pf_cjk and (not sv_cjk or (sv_conf < 0.84 and len(pf_cjk) >= max(2, int(len(sv_cjk) * 0.8)))):
            primary, alt = pf_txt, sv_txt

        sub = srt.Subtitle(
            index=idx,
            start=timedelta(seconds=float(seg["start"])),
            end=timedelta(seconds=float(seg["end"])),
            content=primary or pf_txt or sv_txt,
        )
        sub.confidence = sv_conf
        if alt and _cjk_chars(alt) != _cjk_chars(sub.content):
            sub.paraformer_alt = alt
        subs.append(sub)

    if output_srt_path:
        Path(output_srt_path).write_text(srt.compose(subs, reindex=False), encoding="utf-8")

    logger.info(
        "[PARAFORMER_ZH] Transcribed %d Chinese cues in %.2fs.",
        len(subs),
        time.monotonic() - t0,
    )
    return subs


def enrich_segments_with_paraformer(
    segments: Sequence[Dict[str, Any]],
    audio_path: str | Path,
) -> List[Dict[str, Any]]:
    """Attach Paraformer-large-zh co-recognition & rescue missed whispered Chinese cues."""
    ready, _ = check_paraformer_readiness()
    if not ready or not segments:
        return [dict(s) for s in segments]

    t0 = time.monotonic()
    try:
        mono = _load_mono_16k(audio_path)
        if mono.size < 1600:
            return [dict(s) for s in segments]

        out = [dict(s) for s in segments]
        spans = [(float(s["start"]), float(s["end"])) for s in out]
        pf_texts = _decode_spans_paraformer(mono, spans)
        for item, pf_txt in zip(out, pf_texts):
            w_cjk = _cjk_chars(item.get("text", ""))
            p_cjk = _cjk_chars(pf_txt)
            if p_cjk and p_cjk != w_cjk:
                sim = SequenceMatcher(None, w_cjk, p_cjk, autojunk=False).ratio() if w_cjk else 0.0
                if sim >= 0.35:
                    item["paraformer_alt"] = pf_txt

        logger.info(
            "[PARAFORMER_CO_ASR] Enriched %d segments in %.2fs.",
            len(out),
            time.monotonic() - t0,
        )
        return out
    except Exception as exc:
        logger.debug("[PARAFORMER_CO_ASR] Skipped enrichment on error: %s", exc)
        return [dict(s) for s in segments]
