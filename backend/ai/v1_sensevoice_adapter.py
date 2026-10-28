"""
v1_sensevoice_adapter.py - Adapter SenseVoice-Small (INT8 ONNX) & Bộ Hợp Nhất Hybrid (Faster-Whisper + SenseVoice).

Chức năng chính:
1. Suy luận siêu tốc SenseVoice-Small (ONNX INT8 ~228MB) với trích xuất đặc trưng Kaldi FBank + LFR (60ms/frame) + CMVN.
2. Giải mã CTC xuất mốc thời gian từng ký tự/từ theo mili-giây kèm nhãn sự kiện âm thanh (<|BGM|>, <|Laughter|>, <|Speech|>, <|nospeech|>).
3. Chế độ Hybrid (whisper_sensevoice):
   - Giữ nguyên chất lượng ngữ nghĩa, ngữ cảnh và dấu câu của Faster-Whisper Large-v3 Turbo.
   - Dùng SenseVoice-Small để:
     a) Lọc bỏ câu ảo giác (hallucination) ở đoạn chỉ có nhạc nền/tiếng cười.
     b) Nắn mốc thời gian đầu/cuối câu (start/end) chuẩn xác theo mili-giây bám sát khẩu hình.
     c) Tách câu quá dài tại đúng khoảng nghỉ lấy hơi thực tế (CTC pause).
     d) Khôi phục (Gap Rescue) các câu cảm thán ngắn bị VAD của Whisper bỏ sót.
4. Chế độ Độc lập (sensevoice_small): Nhận dạng siêu tốc (RTF ~0.01x) tốn < 350MB bộ nhớ.
"""

from __future__ import annotations

import gc
import logging
import os
import re
import time
from datetime import timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

import numpy as np
import srt

from config.paths import repo_root

logger = logging.getLogger(__name__)

DEFAULT_SENSEVOICE_DIR = repo_root() / "models" / "v1" / "sensevoice_small"
SENSEVOICE_FRAME_SHIFT_S = 0.06  # 10ms fbank * 6 LFR shift = 60ms per CTC frame

NON_SPEECH_EVENTS = {"<|BGM|>", "<|Laughter|>", "<|Applause|>", "<|Cry|>", "<|Cough|>", "<|Sneeze|>", "<|nospeech|>"}
PUNCTUATION_CHARS = set("，。！？；：、,.!?;:")


def _is_cjk(char: str) -> bool:
    return any(("\u3400" <= c <= "\u4dbf") or ("\u4e00" <= c <= "\u9fff") for c in char)


def _strip_punct(text: str) -> str:
    return "".join(c for c in text if c not in PUNCTUATION_CHARS and not c.isspace())


def is_sensevoice_enabled() -> bool:
    """Kiểm tra xem chế độ ASR hiện tại có sử dụng SenseVoice (Hybrid hoặc Standalone) hay không."""
    try:
        from v1_feature_flags import get_feature_flags
        flags = get_feature_flags()
        model = str(flags.get("V1_ASR_MODEL", "")).strip().lower()
        if model in ("whisper_sensevoice", "hybrid", "sensevoice_small", "sensevoice"):
            return True
    except Exception:
        pass
    env_model = os.getenv("V1_ASR_MODEL", "").strip().lower()
    return env_model in ("whisper_sensevoice", "hybrid", "sensevoice_small", "sensevoice")


def check_sensevoice_readiness(ignore_flag: bool = True) -> Tuple[bool, str]:
    """Kiểm tra trọng số ONNX, bảng token và thư viện runtime cho SenseVoice-Small."""
    if not ignore_flag and not is_sensevoice_enabled():
        return False, "SenseVoice chưa được chọn trong cấu hình V1_ASR_MODEL"

    try:
        import onnxruntime  # noqa: F401
        import torch  # noqa: F401
        import torchaudio  # noqa: F401
    except ImportError as err:
        return False, f"Thiếu thư viện runtime cho SenseVoice: {err}"

    model_file = DEFAULT_SENSEVOICE_DIR / "model.int8.onnx"
    if not model_file.is_file():
        model_file = DEFAULT_SENSEVOICE_DIR / "model.onnx"
    tokens_file = DEFAULT_SENSEVOICE_DIR / "tokens.txt"

    if not model_file.is_file():
        return False, f"Chưa tìm thấy trọng số SenseVoice ONNX tại {DEFAULT_SENSEVOICE_DIR}"
    if not tokens_file.is_file():
        return False, f"Chưa tìm thấy file tokens.txt tại {DEFAULT_SENSEVOICE_DIR}"

    return True, f"Sẵn sàng ({model_file.name}, 228MB INT8 ONNX)"


def _load_tokens(tokens_path: Path) -> Dict[int, str]:
    tokens: Dict[int, str] = {}
    for line in tokens_path.read_text(encoding="utf-8").splitlines():
        raw = line.rstrip("\r\n")
        if not raw:
            continue
        parts = raw.rsplit(" ", 1)
        if len(parts) == 2:
            piece, idx_str = parts
            try:
                tokens[int(idx_str)] = piece
            except ValueError:
                continue
    return tokens


def _load_audio_16k_mono(audio_path: str | Path) -> np.ndarray:
    """Tải audio về mảng float32 16kHz mono trong biên độ [-32768, 32767] chuẩn Kaldi (không cần nạp PyTorch)."""
    import math
    import soundfile as sf
    from scipy.signal import resample_poly

    path_str = str(audio_path)
    try:
        data, sr = sf.read(path_str, dtype="float32", always_2d=True)
        mono = data.mean(axis=1) if data.shape[1] > 1 else data[:, 0]
        if sr != 16000:
            g = math.gcd(int(sr), 16000)
            mono = resample_poly(mono, 16000 // g, int(sr) // g).astype(np.float32)
        return (mono * 32768.0).astype(np.float32)
    except Exception:
        import torchaudio
        waveform, sr = torchaudio.load(path_str)
        if waveform.shape[0] > 1:
            waveform = waveform.mean(dim=0, keepdim=True)
        if sr != 16000:
            waveform = torchaudio.functional.resample(waveform, sr, 16000)
        return (waveform.squeeze(0).numpy() * 32768.0).astype(np.float32)


def _build_kaldi_mel_banks(
    num_bins: int = 80,
    window_padded: int = 512,
    sample_freq: float = 16000.0,
    low_freq: float = 20.0,
    high_freq: float = 8000.0,
) -> np.ndarray:
    num_fft_bins = window_padded // 2
    fft_bin_width = sample_freq / window_padded
    mel_low = 1127.0 * np.log(1.0 + low_freq / 700.0)
    mel_high = 1127.0 * np.log(1.0 + high_freq / 700.0)
    mel_delta = (mel_high - mel_low) / (num_bins + 1)
    bins = np.arange(num_bins, dtype=np.float32)[:, None]
    left_mel = mel_low + bins * mel_delta
    center_mel = mel_low + (bins + 1.0) * mel_delta
    right_mel = mel_low + (bins + 2.0) * mel_delta
    freqs = fft_bin_width * np.arange(num_fft_bins, dtype=np.float32)[None, :]
    mel = 1127.0 * np.log(1.0 + freqs / 700.0)
    up_slope = (mel - left_mel) / (center_mel - left_mel)
    down_slope = (right_mel - mel) / (right_mel - center_mel)
    return np.maximum(0.0, np.minimum(up_slope, down_slope)).astype(np.float32)


_KALDI_MEL_BANKS_80 = _build_kaldi_mel_banks()
_KALDI_HAMMING_400 = (0.54 - 0.46 * np.cos(2.0 * np.pi * np.arange(400, dtype=np.float32) / 399.0)).astype(np.float32)
_SENSEVOICE_ENGINE_CACHE: Dict[str, "SenseVoiceEngine"] = {}


def _extract_lfr_cmvn_features(
    samples_16k: np.ndarray,
    lfr_m: int,
    lfr_n: int,
    neg_mean: np.ndarray,
    inv_stddev: np.ndarray,
) -> np.ndarray:
    """Trích xuất Kaldi 80-dim Mel-FBank + LFR stacking + CMVN normalization bằng NumPy thuần."""
    if samples_16k.size < 400:
        return np.zeros((0, lfr_m * 80), dtype=np.float32)

    num_frames = 1 + (samples_16k.size - 400) // 160
    if num_frames <= 0:
        return np.zeros((0, lfr_m * 80), dtype=np.float32)

    frame_idx = np.arange(400)[None, :] + (np.arange(num_frames) * 160)[:, None]
    frames = samples_16k[frame_idx].astype(np.float32, copy=True)
    # Remove DC offset
    frames -= np.mean(frames, axis=1, keepdims=True)
    # Pre-emphasis (0.97)
    frames[:, 1:] -= 0.97 * frames[:, :-1]
    frames[:, 0] *= (1.0 - 0.97)
    # Hamming window
    frames *= _KALDI_HAMMING_400[None, :]
    # Power spectrum via 512-point RFFT (keep first 256 bins per Kaldi spec)
    spec = np.abs(np.fft.rfft(frames, n=512, axis=1)[:, :256]) ** 2
    fbank = np.log(np.maximum(spec @ _KALDI_MEL_BANKS_80.T, np.finfo(np.float32).eps)).astype(np.float32)

    if num_frames < lfr_m:
        pad = np.repeat(fbank[-1:, :], lfr_m - num_frames, axis=0)
        fbank = np.concatenate([fbank, pad], axis=0)
        num_frames = fbank.shape[0]

    t_lfr = (num_frames - lfr_m) // lfr_n + 1
    if t_lfr <= 0:
        return np.zeros((0, lfr_m * 80), dtype=np.float32)

    indices = np.arange(lfr_m)[None, :] + (np.arange(t_lfr) * lfr_n)[:, None]
    lfr_feats = fbank[indices].reshape(t_lfr, lfr_m * 80).astype(np.float32)
    lfr_feats = (lfr_feats + neg_mean) * inv_stddev
    return lfr_feats


class SenseVoiceEngine:
    """Quản lý phiên chạy ONNX của SenseVoice-Small."""

    def __init__(self, model_dir: Path = DEFAULT_SENSEVOICE_DIR):
        import onnxruntime as ort

        self.model_dir = Path(model_dir)
        model_file = self.model_dir / "model.int8.onnx"
        if not model_file.is_file():
            model_file = self.model_dir / "model.onnx"
        tokens_file = self.model_dir / "tokens.txt"

        sess_opts = ort.SessionOptions()
        sess_opts.intra_op_num_threads = max(2, min(4, (os.cpu_count() or 4) // 2))
        sess_opts.inter_op_num_threads = 1
        sess_opts.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
        try:
            sess_opts.add_session_config_entry("session.intra_op.allow_spinning", "0")
            sess_opts.add_session_config_entry("session.inter_op.allow_spinning", "0")
        except Exception:
            pass

        # SenseVoice INT8 ONNX chạy cực nhanh trên CPUExecutionProvider (~0.1s/clip)
        # và hoàn toàn không chiếm VRAM của GPU RTX 4050!
        self.session = ort.InferenceSession(
            str(model_file),
            sess_options=sess_opts,
            providers=["CPUExecutionProvider"],
        )
        meta = self.session.get_modelmeta().custom_metadata_map
        self.lfr_m = int(meta.get("lfr_window_size", 7))
        self.lfr_n = int(meta.get("lfr_window_shift", 6))
        self.neg_mean = np.array([float(x) for x in meta["neg_mean"].split(",")], dtype=np.float32)
        self.inv_stddev = np.array([float(x) for x in meta["inv_stddev"].split(",")], dtype=np.float32)
        self.lang_auto = int(meta.get("lang_auto", 0))
        self.lang_zh = int(meta.get("lang_zh", 3))
        self.with_itn = int(meta.get("with_itn", 14))
        self.tokens = _load_tokens(tokens_file)

    @classmethod
    def get_cached(cls, model_dir: Path = DEFAULT_SENSEVOICE_DIR) -> "SenseVoiceEngine":
        key = str(Path(model_dir).resolve())
        eng = _SENSEVOICE_ENGINE_CACHE.get(key)
        if eng is None:
            eng = cls(model_dir=model_dir)
            _SENSEVOICE_ENGINE_CACHE[key] = eng
        return eng

    def infer_window(
        self,
        samples_16k: np.ndarray,
        offset_s: float = 0.0,
        language_id: Optional[int] = None,
    ) -> Dict[str, Any]:
        """Suy luận một cửa sổ âm thanh, trả về nhãn sự kiện và danh sách token CTC kèm mốc thời gian."""
        feats = _extract_lfr_cmvn_features(
            samples_16k, self.lfr_m, self.lfr_n, self.neg_mean, self.inv_stddev
        )
        if feats.shape[0] == 0:
            return {
                "offset_s": offset_s,
                "duration_s": len(samples_16k) / 16000.0,
                "lang_tag": "<|nospeech|>",
                "emotion_tag": "<|NEUTRAL|>",
                "event_tag": "<|nospeech|>",
                "tokens": [],
            }

        x = feats[None, :, :]
        x_len = np.array([feats.shape[0]], dtype=np.int32)
        lang = np.array([self.lang_auto if language_id is None else language_id], dtype=np.int32)
        text_norm = np.array([self.with_itn], dtype=np.int32)

        logits = self.session.run(
            None,
            {
                "x": x,
                "x_length": x_len,
                "language": lang,
                "text_norm": text_norm,
            },
        )[0][0]  # shape: [T + 4, vocab_size]

        token_ids = np.argmax(logits, axis=-1)

        lang_tag = self.tokens.get(int(token_ids[0]), "<|auto|>") if len(token_ids) > 0 else "<|auto|>"
        emotion_tag = self.tokens.get(int(token_ids[1]), "<|NEUTRAL|>") if len(token_ids) > 1 else "<|NEUTRAL|>"
        event_tag = self.tokens.get(int(token_ids[2]), "<|Speech|>") if len(token_ids) > 2 else "<|Speech|>"

        decoded_tokens: List[Dict[str, Any]] = []
        inline_events: List[str] = []

        def _frame_prob(f_idx: int, win_tid: int) -> float:
            row = logits[f_idx]
            max_val = float(row[win_tid])
            return float(1.0 / max(float(np.sum(np.exp(row - max_val))), 1e-9))

        T_total = len(token_ids)
        idx = 4
        while idx < T_total:
            tid = int(token_ids[idx])
            if tid == 0:
                idx += 1
                continue
            start_idx = idx
            best_prob = _frame_prob(idx, tid)
            while idx + 1 < T_total and int(token_ids[idx + 1]) == tid:
                idx += 1
                p_cur = _frame_prob(idx, tid)
                if p_cur > best_prob:
                    best_prob = p_cur
            end_idx = idx
            piece = self.tokens.get(tid, "")
            if piece.startswith("<|") and piece.endswith("|>"):
                inline_events.append(piece)
            elif piece not in ("<unk>", "<s>", "</s>", ""):
                clean_piece = piece.replace("▁", " ")
                if clean_piece.strip():
                    st = offset_s + max(0.0, (start_idx - 4) * SENSEVOICE_FRAME_SHIFT_S)
                    et = offset_s + max(
                        (start_idx - 4) * SENSEVOICE_FRAME_SHIFT_S + 0.04,
                        (end_idx - 4 + 1) * SENSEVOICE_FRAME_SHIFT_S,
                    )
                    decoded_tokens.append(
                        {
                            "text": clean_piece,
                            "start": round(st, 3),
                            "end": round(et, 3),
                            "prob": round(best_prob, 4),
                        }
                    )
            idx += 1

        return {
            "offset_s": round(offset_s, 3),
            "duration_s": round(len(samples_16k) / 16000.0, 3),
            "lang_tag": lang_tag,
            "emotion_tag": emotion_tag,
            "event_tag": event_tag,
            "inline_events": inline_events,
            "tokens": decoded_tokens,
        }


def analyze_audio_sensevoice(
    audio_path: str | Path,
    window_s: float = 25.0,
    overlap_s: float = 0.6,
) -> Dict[str, Any]:
    """
    Phân tích toàn bộ file audio bằng SenseVoice-Small theo cửa sổ trượt an toàn.
    Trả về danh sách toàn bộ token CTC có mốc thời gian mili-giây và thông tin sự kiện âm học.
    """
    t0 = time.monotonic()
    samples = _load_audio_16k_mono(audio_path)
    total_dur_s = len(samples) / 16000.0
    engine = SenseVoiceEngine.get_cached()

    windows_info: List[Dict[str, Any]] = []
    all_tokens: List[Dict[str, Any]] = []

    if total_dur_s <= window_s + 2.0:
        w_res = engine.infer_window(samples, offset_s=0.0)
        windows_info.append(w_res)
        all_tokens.extend(w_res["tokens"])
    else:
        step_s = max(5.0, window_s - overlap_s)
        cursor_s = 0.0
        while cursor_s < total_dur_s:
            end_s = min(total_dur_s, cursor_s + window_s)
            s_idx = int(round(cursor_s * 16000))
            e_idx = int(round(end_s * 16000))
            chunk = samples[s_idx:e_idx]
            if len(chunk) >= 800:
                w_res = engine.infer_window(chunk, offset_s=cursor_s)
                windows_info.append(w_res)
                # Khử trùng lặp token ở vùng biên overlap
                boundary_cutoff = cursor_s + (overlap_s * 0.5) if cursor_s > 0 else 0.0
                for tok in w_res["tokens"]:
                    if cursor_s > 0 and tok["start"] < boundary_cutoff:
                        continue
                    if all_tokens and tok["start"] < all_tokens[-1]["end"] - 0.02 and tok["text"].strip() == all_tokens[-1]["text"].strip():
                        continue
                    all_tokens.append(tok)
            if end_s >= total_dur_s:
                break
            cursor_s += step_s

    elapsed = time.monotonic() - t0
    logger.info(
        "[SENSEVOICE] Phân tích xong %.1fs audio trong %.2fs (%d tokens CTC, %d cửa sổ).",
        total_dur_s,
        elapsed,
        len(all_tokens),
        len(windows_info),
    )
    return {
        "duration_s": round(total_dur_s, 3),
        "elapsed_s": round(elapsed, 3),
        "tokens": all_tokens,
        "windows": windows_info,
    }


def _join_sv_tokens(tokens: Sequence[Dict[str, Any]]) -> str:
    res = ""
    for t in tokens:
        piece = str(t.get("text", ""))
        if not piece:
            continue
        if not res:
            res = piece.lstrip()
            continue
        if (_is_cjk(res[-1:]) or res[-1:] in PUNCTUATION_CHARS) and (
            _is_cjk(piece.lstrip()[:1]) or piece.lstrip()[:1] in PUNCTUATION_CHARS
        ):
            res += piece.lstrip()
        elif piece.startswith(" ") or res.endswith(" "):
            res += piece
        else:
            res += " " + piece
    return res.strip()


def group_sensevoice_tokens_to_segments(
    tokens: Sequence[Dict[str, Any]],
    max_cjk_chars: int = 24,
    max_gap_s: float = 0.65,
    max_duration_s: float = 5.0,
) -> List[Dict[str, Any]]:
    """Gom nhóm danh sách token CTC của SenseVoice thành các câu phụ đề hoàn chỉnh."""
    segments: List[Dict[str, Any]] = []
    cur_tokens: List[Dict[str, Any]] = []

    for tok in tokens:
        txt = str(tok.get("text", "")).strip()
        if not txt:
            continue
        if not cur_tokens:
            cur_tokens.append(tok)
            continue

        gap = float(tok["start"]) - float(cur_tokens[-1]["end"])
        cur_text = _join_sv_tokens(cur_tokens)
        vis_len = len(_strip_punct(cur_text))
        dur = float(tok["end"]) - float(cur_tokens[0]["start"])
        ends_sentence = cur_text[-1:] in set("。！？!?；;")
        ends_clause = cur_text[-1:] in set("，,：:") and (vis_len >= 12 or dur >= 2.5)

        if (
            gap > max_gap_s
            or dur > max_duration_s
            or vis_len >= max_cjk_chars
            or ends_sentence
            or ends_clause
        ):
            if _strip_punct(cur_text):
                segments.append(
                    {
                        "start": round(float(cur_tokens[0]["start"]), 3),
                        "end": round(float(cur_tokens[-1]["end"]), 3),
                        "text": cur_text,
                    }
                )
            cur_tokens = [tok]
        else:
            cur_tokens.append(tok)

    if cur_tokens:
        cur_text = _join_sv_tokens(cur_tokens)
        if _strip_punct(cur_text):
            segments.append(
                {
                    "start": round(float(cur_tokens[0]["start"]), 3),
                    "end": round(float(cur_tokens[-1]["end"]), 3),
                    "text": cur_text,
                }
            )
    return segments


def _is_pure_nonspeech_window(
    start_s: float,
    end_s: float,
    sv_tokens: Sequence[Dict[str, Any]],
    sv_windows: Sequence[Dict[str, Any]],
) -> bool:
    """Kiểm tra xem khoảng [start_s, end_s] có phải hoàn toàn không có tiếng người nói theo SenseVoice không."""
    overlapping_tokens = [
        t for t in sv_tokens
        if float(t["end"]) > start_s - 0.15 and float(t["start"]) < end_s + 0.15
        and _strip_punct(str(t.get("text", "")))
    ]
    if overlapping_tokens:
        return False

    for w in sv_windows:
        w_st = float(w.get("offset_s", 0.0))
        w_et = w_st + float(w.get("duration_s", 0.0))
        if w_et > start_s and w_st < end_s:
            ev = str(w.get("event_tag", ""))
            lang = str(w.get("lang_tag", ""))
            if ev in NON_SPEECH_EVENTS or lang == "<|nospeech|>":
                return True
    return len(overlapping_tokens) == 0


def refine_whisper_with_sensevoice(
    whisper_segments: Sequence[Dict[str, Any]],
    audio_path: str | Path,
    original_audio_path: Optional[str | Path] = None,
) -> List[Dict[str, Any]]:
    """
    Bộ Hợp Nhất Thông Minh (Hybrid Refiner):
    Kết hợp nội dung ngữ nghĩa của Faster-Whisper với mốc thời gian CTC 60ms và bộ lọc sự kiện âm thanh của SenseVoice-Small.
    """
    if not whisper_segments:
        # Nếu Whisper không bắt được câu nào, thử dùng SenseVoice quét xem có câu thoại ngắn bị sót không
        sv_data = analyze_audio_sensevoice(audio_path)
        rescued = group_sensevoice_tokens_to_segments(sv_data.get("tokens", []))
        return rescued

    try:
        from .v1_conditional_asr import is_hallucination
    except ImportError:
        from ai.v1_conditional_asr import is_hallucination

    sv_data = analyze_audio_sensevoice(audio_path)
    sv_tokens = sv_data.get("tokens", [])
    sv_windows = sv_data.get("windows", [])

    if not sv_tokens:
        logger.info("[HYBRID_ASR] SenseVoice không phát hiện token CTC nào; giữ nguyên kết quả Faster-Whisper.")
        return [dict(s) for s in whisper_segments]

    refined: List[Dict[str, Any]] = []
    dropped_hallucinations = 0
    snapped_count = 0
    split_count = 0

    num_segs = len(whisper_segments)
    for idx, seg in enumerate(whisper_segments):
        w_start = float(seg["start"])
        w_end = float(seg["end"])
        w_text = str(seg.get("text", "")).strip()
        if not w_text or w_end <= w_start:
            continue

        prev_w_end = float(whisper_segments[idx - 1]["end"]) if idx > 0 else 0.0
        next_w_start = float(whisper_segments[idx + 1]["start"]) if idx + 1 < num_segs else float("inf")

        # --- LỚP 1: ACOUSTIC EVENT & HALLUCINATION GUARD ---
        is_no_speech = _is_pure_nonspeech_window(w_start, w_end, sv_tokens, sv_windows)
        if is_no_speech and is_hallucination(w_text):
            dropped_hallucinations += 1
            logger.info(
                "[HYBRID_ASR] Loại bỏ câu ảo giác tại [%.2fs - %.2fs]: %s (SenseVoice xác nhận không có tiếng nói)",
                w_start,
                w_end,
                w_text,
            )
            continue

        # --- LỚP 2: CTC MILLISECOND BOUNDARY SNAPPING ---
        # Tìm các token thoại của SenseVoice thuộc về câu hiện tại (không lấn sang câu lân cận)
        search_left = max(prev_w_end + 0.02, w_start - 0.35)
        search_right = min(next_w_start - 0.02, w_end + 0.35)

        cand_tokens = [
            t for t in sv_tokens
            if float(t["end"]) > search_left
            and float(t["start"]) < search_right
            and _strip_punct(str(t.get("text", "")))
        ]

        new_start = w_start
        new_end = w_end

        if cand_tokens:
            w_clean = _strip_punct(w_text)
            sv_clean = _strip_punct("".join(str(t["text"]) for t in cand_tokens))
            first_tok = cand_tokens[0]
            last_tok = cand_tokens[-1]
            first_tok_clean = _strip_punct(str(first_tok["text"]))
            last_tok_clean = _strip_punct(str(last_tok["text"]))

            char_ratio = len(sv_clean) / max(1, len(w_clean))
            # Kiểm tra khớp ký tự đầu hoặc độ phủ câu tương đương
            first_char_match = bool(
                w_clean and first_tok_clean and (first_tok_clean[0] in w_clean[:2] or w_clean[0] in first_tok_clean)
            )
            last_char_match = bool(
                w_clean and last_tok_clean and (last_tok_clean[-1] in w_clean[-2:] or w_clean[-1] in last_tok_clean)
            )

            target_start = max(0.0, float(first_tok["start"]) - 0.04)
            target_end = float(last_tok["end"]) + 0.06

            # Nắn điểm đầu (start):
            # - Nếu khớp chữ đầu tiên: cho phép kéo sớm lên tới 0.35s hoặc dời trễ (cắt khoảng lặng đầu) tới 0.65s
            # - Nếu không khớp chữ đầu nhưng độ phủ câu >= 65%: chỉ cắt khoảng lặng thừa đầu câu tối đa 0.45s
            start_diff = target_start - w_start
            if first_char_match and -0.35 <= start_diff <= 0.65:
                new_start = target_start
            elif char_ratio >= 0.65 and 0.04 <= start_diff <= 0.45:
                new_start = target_start

            # Nắn điểm cuối (end):
            # - Nếu khớp chữ cuối: cho phép thu gọn đuôi ngân thừa tới 0.70s hoặc nới thêm 0.35s
            # - Nếu không khớp chữ cuối nhưng độ phủ câu >= 65%: thu gọn khoảng lặng cuối tối đa 0.45s
            end_diff = target_end - w_end
            if last_char_match and -0.70 <= end_diff <= 0.35:
                new_end = target_end
            elif char_ratio >= 0.65 and -0.45 <= end_diff <= -0.04:
                new_end = target_end

            # Đảm bảo tính hợp lệ hình học thời gian
            if refined:
                new_start = max(new_start, float(refined[-1]["end"]) + 0.01)
            if new_end - new_start < 0.35:
                new_start = w_start
                new_end = max(w_end, new_start + 0.35)

            if abs(new_start - w_start) >= 0.03 or abs(new_end - w_end) >= 0.03:
                snapped_count += 1

        # --- LỚP 3: ACOUSTIC PAUSE SPLITTING CHO CÂU QUÁ DÀI ---
        seg_dur = new_end - new_start
        w_vis_len = len(_strip_punct(w_text))
        if seg_dur > 5.2 and w_vis_len > 22 and len(cand_tokens) >= 6:
            # Tìm khoảng nghỉ âm học lớn nhất giữa các token SenseVoice ở phần giữa câu
            best_pause_gap = 0.0
            best_pause_time = None
            for t_i in range(2, len(cand_tokens) - 2):
                gap = float(cand_tokens[t_i]["start"]) - float(cand_tokens[t_i - 1]["end"])
                rel_pos = (float(cand_tokens[t_i]["start"]) - new_start) / seg_dur
                if 0.25 <= rel_pos <= 0.75 and gap > best_pause_gap:
                    best_pause_gap = gap
                    best_pause_time = (float(cand_tokens[t_i - 1]["end"]) + float(cand_tokens[t_i]["start"])) / 2.0

            # Tìm dấu phẩy/chấm giữa câu Whisper để tách tương ứng
            mid_char = len(w_text) // 2
            punct_indices = [
                m.end() for m in re.finditer(r"[，。！？；,!;?]", w_text)
                if int(len(w_text) * 0.25) <= m.end() <= int(len(w_text) * 0.75)
            ]
            if best_pause_gap >= 0.38 and best_pause_time is not None and punct_indices:
                split_char_idx = min(punct_indices, key=lambda p: abs(p - mid_char))
                left_text = w_text[:split_char_idx].strip()
                right_text = w_text[split_char_idx:].strip()
                if len(_strip_punct(left_text)) >= 4 and len(_strip_punct(right_text)) >= 4:
                    split_t = round(best_pause_time, 3)
                    if split_t - new_start >= 1.0 and new_end - split_t >= 1.0:
                        refined.append({"start": round(new_start, 3), "end": round(split_t - 0.02, 3), "text": left_text})
                        refined.append({"start": round(split_t + 0.02, 3), "end": round(new_end, 3), "text": right_text})
                        split_count += 1
                        continue

        refined.append(
            {
                "start": round(new_start, 3),
                "end": round(new_end, 3),
                "text": w_text,
            }
        )

    # --- LỚP 4: GAP RESCUE (KHÔI PHỤC CÂU THOẠI NGẮN BỊ WHISPER BỎ SÓT) ---
    rescued_segments: List[Dict[str, Any]] = []
    total_dur = float(sv_data.get("duration_s", 0.0))
    check_intervals: List[Tuple[float, float]] = []
    cursor = 0.0
    for seg in refined:
        if float(seg["start"]) - cursor >= 1.8:
            check_intervals.append((cursor, float(seg["start"])))
        cursor = max(cursor, float(seg["end"]))
    if total_dur - cursor >= 1.8:
        check_intervals.append((cursor, total_dur))

    for g_start, g_end in check_intervals:
        gap_tokens = [
            t for t in sv_tokens
            if float(t["start"]) >= g_start + 0.18
            and float(t["end"]) <= g_end - 0.18
            and _strip_punct(str(t.get("text", "")))
        ]
        if not gap_tokens:
            continue
        gap_segs = group_sensevoice_tokens_to_segments(gap_tokens)
        for gs in gap_segs:
            gs_text = str(gs["text"]).strip()
            gs_clean = _strip_punct(gs_text)
            cjk_count = sum(1 for c in gs_clean if _is_cjk(c))
            # Chỉ khôi phục khi có >= 2 chữ Hán rõ ràng, độ tự tin CTC cao và không phải ảo giác
            seg_toks = [
                t for t in gap_tokens
                if float(t["start"]) >= float(gs["start"]) - 0.05 and float(t["end"]) <= float(gs["end"]) + 0.05
            ]
            avg_prob = sum(float(t.get("prob", 0.0)) for t in seg_toks) / max(1, len(seg_toks))
            if cjk_count >= 2 and avg_prob >= 0.78 and not is_hallucination(gs_text):
                rescued_segments.append(gs)

    if rescued_segments:
        refined.extend(rescued_segments)
        refined.sort(key=lambda item: (float(item["start"]), float(item["end"])))

    logger.info(
        "[HYBRID_ASR] Hoàn tất hợp nhất Whisper + SenseVoice: %d câu (Nắn biên CTC: %d, Lọc ảo giác: %d, Tách câu dài: %d, Cứu câu sót: %d)",
        len(refined),
        snapped_count,
        dropped_hallucinations,
        split_count,
        len(rescued_segments),
    )
    print(
        f"✨ [Hybrid ASR] SenseVoice-Small đã tinh chỉnh {snapped_count}/{len(refined)} mốc thời gian CTC (60ms), "
        f"lọc {dropped_hallucinations} câu nhạc ảo, tách {split_count} câu dài, cứu {len(rescued_segments)} câu sót."
    )
    return refined


def transcribe_audio_sensevoice(
    audio_path: str | Path,
    output_srt_path: Optional[str | Path] = None,
) -> List[srt.Subtitle]:
    """
    Thực thi nhận dạng âm thanh độc lập bằng SenseVoice-Small (chế độ siêu tốc sensevoice_small).
    """
    ready, reason = check_sensevoice_readiness(ignore_flag=True)
    if not ready:
        raise RuntimeError(f"SenseVoice-Small chưa sẵn sàng: {reason}")

    sv_data = analyze_audio_sensevoice(audio_path)
    segments = group_sensevoice_tokens_to_segments(sv_data.get("tokens", []))

    subtitles: List[srt.Subtitle] = []
    for idx, seg in enumerate(segments, start=1):
        subtitles.append(
            srt.Subtitle(
                index=idx,
                start=timedelta(seconds=float(seg["start"])),
                end=timedelta(seconds=float(seg["end"])),
                content=str(seg["text"]).strip(),
            )
        )

    if output_srt_path:
        out_p = Path(output_srt_path)
        out_p.parent.mkdir(parents=True, exist_ok=True)
        out_p.write_text(srt.compose(subtitles, reindex=False), encoding="utf-8")

    gc.collect()
    return subtitles
