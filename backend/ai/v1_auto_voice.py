"""
v1_auto_voice.py - Khóa giọng đọc video duy nhất (Tool V1 - Codex Plan)

Nguyên tắc:
1. Mỗi video chỉ chọn giọng MỘT LẦN dựa trên người nói đầu tiên được nhận diện đủ tin cậy.
2. Khóa giọng đó cho toàn bộ các câu trong video từ đầu đến cuối.
3. Người nói tiếp theo (dù đổi nam/nữ) TUYỆT ĐỐI KHÔNG làm đổi giọng video.
4. Quy tắc ánh xạ:
   - Người nói đầu là NỮ -> Khóa Chí Mai (RVC chi-mai, fallback CapCut/Edge nếu thiếu runtime RVC).
   - Người nói đầu là NAM -> Khóa Thanh Niên Tự Tin (CapCut capcut-BV075_streaming, param: BV075_streaming).
   - Không xác định / nhiễu / quá ngắn -> Fallback về giọng mặc định cấu hình; ghi log rõ ràng.
5. Snapshot & Persistence:
   - Lưu lựa chọn vào voice_lock.json trong thư mục job (out_dir).
   - Khi resume, retry, hoặc chạy nền: Luôn đọc trực tiếp từ voice_lock.json đã lưu,
     đảm bảo không bị nhảy giọng kể cả khi cấu hình toàn cục thay đổi.
"""

from __future__ import annotations

import json
import logging
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

import numpy as np

logger = logging.getLogger("v1_auto_voice")

VOICE_FEMALE_ID = "chi-mai"
VOICE_MALE_ID = "capcut-BV075_streaming"
VOICE_MALE_PARAM = "BV075_streaming"
VOICE_LOCK_FILENAME = "voice_lock.json"

PITCH_BOUNDARY_HZ = 180.0  # < 180 Hz la nam, >= 180 Hz la nu
MIN_VOICED_FRAMES = 12     # It nhat ~12 voiced frames (~120ms am thanh huu thanh)
MIN_SEGMENT_DURATION = 0.4  # Do dai toi thieu de phan tich (giay)
CONFIDENCE_THRESHOLD = 0.70  # Nguong tin cay de khoa theo gioi tinh vs fallback


def _get_start_sec(seg: Any) -> float:
    if hasattr(seg, "start"):
        s = seg.start
        return s.total_seconds() if hasattr(s, "total_seconds") else float(s)
    if isinstance(seg, dict) and "start" in seg:
        s = seg["start"]
        return s.total_seconds() if hasattr(s, "total_seconds") else float(s)
    return 0.0


def _get_end_sec(seg: Any) -> float:
    if hasattr(seg, "end"):
        e = seg.end
        return e.total_seconds() if hasattr(e, "total_seconds") else float(e)
    if isinstance(seg, dict) and "end" in seg:
        e = seg["end"]
        return e.total_seconds() if hasattr(e, "total_seconds") else float(e)
    return 0.0


def _get_content(seg: Any) -> str:
    if hasattr(seg, "content"):
        return str(seg.content or "")
    if isinstance(seg, dict) and "content" in seg:
        return str(seg["content"] or "")
    if isinstance(seg, dict) and "text" in seg:
        return str(seg["text"] or "")
    return ""


def _get_index(seg: Any, default: int = 1) -> int:
    if hasattr(seg, "index"):
        try:
            return int(seg.index)
        except Exception:
            pass
    if isinstance(seg, dict) and "index" in seg:
        try:
            return int(seg["index"])
        except Exception:
            pass
    return default


def extract_segment_audio_slice(
    audio_path: str | Path,
    start_sec: float,
    end_sec: float,
    target_sr: int = 16000,
) -> Optional[Tuple[np.ndarray, int]]:
    """Trich xuat doan mau audio [start_sec, end_sec] tai sample rate muc tieu."""
    if not audio_path or not os.path.exists(str(audio_path)):
        return None
    try:
        import librosa
        duration = max(float(end_sec) - float(start_sec), 0.1)
        y, sr = librosa.load(
            str(audio_path),
            sr=target_sr,
            offset=max(0.0, float(start_sec)),
            duration=duration,
        )
        if len(y) < sr * 0.1:
            return None
        return y, sr
    except Exception as exc:
        logger.debug(
            "Khong the trich xuat audio slice tu %s (%.2f-%.2f): %s",
            audio_path, start_sec, end_sec, exc
        )
        return None


def estimate_f0_pitch(y: np.ndarray, sr: int) -> Tuple[float, int, float, str, float]:
    """
    Uoc luong cao do co ban (F0) su dung Parselmouth (Praat) voi fallback sang librosa.yin.
    Tra ve: (median_f0, voiced_frame_count, confidence, gender, mean_hnr)
    """
    if y is None or len(y) < sr * 0.1:
        return 0.0, 0, 0.0, "unknown", -99.0

    voiced_f0 = np.array([])
    mean_hnr = -99.0
    # 1. Parselmouth (Praat) - tieu chuan vang speech science
    try:
        import parselmouth
        snd = parselmouth.Sound(y.astype(np.float64), sampling_frequency=sr)
        # Kiem tra do hai hoa am thanh (Harmonics-to-Noise Ratio - HNR)
        hnr = snd.to_harmonicity()
        mean_hnr = float(parselmouth.praat.call(hnr, 'Get mean', 0, 0))
        # Am thoai con nguoi luon co HNR > 3.0 dB (thuong 10-25 dB). Nhieu trang co HNR < 0 dB.
        if mean_hnr >= 3.0:
            pitch = snd.to_pitch_cc(pitch_floor=75, pitch_ceiling=350, voicing_threshold=0.45)
            v = pitch.selected_array['frequency']
            voiced_f0 = v[(v >= 75) & (v <= 340)]
    except Exception as e:
        logger.debug("Parselmouth gap loi: %s; thu tiep librosa.yin", e)

    # 2. Fallback sang librosa.yin neu parselmouth khong tim thay khung thoai
    if len(voiced_f0) == 0:
        try:
            import librosa
            flatness = float(np.mean(librosa.feature.spectral_flatness(y=y)))
            if flatness <= 0.4:  # Bo qua neu la tieng on (spectral flatness cao)
                f0 = librosa.yin(y, fmin=75, fmax=350, sr=sr)
                voiced_f0 = f0[(f0 >= 75) & (f0 <= 340)]
        except Exception as e:
            logger.debug("librosa.yin gap loi: %s", e)

    if len(voiced_f0) < MIN_VOICED_FRAMES:
        return 0.0, len(voiced_f0), 0.0, "unknown", mean_hnr

    median_f0 = float(np.median(voiced_f0))
    voiced_count = len(voiced_f0)

    # Quy tac phan dinh:
    # Giong nam VN pho bien: 85 - 165 Hz (Nam Minh: 127 Hz, BV075: 165 Hz)
    # Giong nu VN pho bien: 200 - 260 Hz (Hoai My: 202 Hz, Mai BV562: 246 Hz)
    if median_f0 < 175.0:
        gender = "male"
        confidence = min(0.98, 0.80 + max(0.0, 165.0 - median_f0) * 0.005)
    elif median_f0 >= 185.0:
        gender = "female"
        confidence = min(0.98, 0.80 + max(0.0, median_f0 - 195.0) * 0.005)
    else:
        # Vung giap ranh (175 - 185 Hz)
        gender = "male" if median_f0 < 180.0 else "female"
        confidence = 0.55

    # Dieu chinh theo so luong voiced frame de tang do tin cay
    frame_scale = min(1.0, voiced_count / 30.0)
    confidence = float(np.clip(confidence * frame_scale, 0.0, 1.0))

    return median_f0, voiced_count, confidence, gender, mean_hnr


def detect_first_speaker_gender(
    audio_path: Optional[str | Path],
    srt_segments: Sequence[Any],
    backup_audio_path: Optional[str | Path] = None,
    **kwargs,
) -> Tuple[str, float, float, int]:
    """
    Nhan dien gioi tinh DUY NHAT dua tren cau thoai DAU TIEN thuc su trong video (Codex Plan).
    TUYET DOI KHONG nhay sang cau thu 2, 3 de tranh nham nguoi noi khac gioi xuat hien sau.
    Thu ca vocals_audio va original_audio tren dung cau dau tien de co tin hieu am hoc tot nhat.
    Neu cau dau khong du tin cay (conf < CONFIDENCE_THRESHOLD), kich hoat fallback an toan.
    """
    if not srt_segments:
        logger.info("Khong co phan doan phu de de nhan dien nguoi noi.")
        return "unknown", 0.0, 0.0, -1

    first_candidate = None
    for seg in srt_segments:
        start_s = _get_start_sec(seg)
        end_s = _get_end_sec(seg)
        content = _get_content(seg).strip()
        idx = _get_index(seg, 1)
        duration = max(0.0, end_s - start_s)
        # Bắt ngay câu thoại đầu tiên có từ ngữ, không nhảy qua câu ngắn sang người thứ 2
        if re.search(r'\w', content):
            first_candidate = (idx, start_s, end_s, content, duration)
            break

    if not first_candidate:
        logger.info("Khong tim thay doan phu de co loi thoai o dau video.")
        return "unknown", 0.0, 0.0, -1

    idx, start_s, end_s, content, dur = first_candidate
    logger.info("Phan tich am hoc cho cau thoai dau tien #%d [%.2f-%.2f]: '%s'", idx, start_s, end_s, content[:40])

    primary_path = Path(audio_path) if audio_path and Path(audio_path).is_file() else None
    secondary_path = Path(backup_audio_path) if backup_audio_path and Path(backup_audio_path).is_file() else None

    if not primary_path and not secondary_path:
        logger.warning("Khong tim thay file am thanh hop le de phan tich giong.")
        return "unknown", 0.0, 0.0, idx

    best_result = ("unknown", 0.0, 0.0, idx)

    # 1. Thu tren vocals_path (am thanh giong sach tach tu Demucs)
    if primary_path:
        slice_voc = extract_segment_audio_slice(primary_path, start_s, end_s)
        if slice_voc is not None:
            y_voc, sr_voc = slice_voc
            f0_voc, cnt_voc, conf_voc, gen_voc, hnr_voc = estimate_f0_pitch(y_voc, sr_voc)
            logger.debug(
                "Cau #%d (vocals): F0=%.1f, count=%d, conf=%.2f, gender=%s, HNR=%.1f",
                idx, f0_voc, cnt_voc, conf_voc, gen_voc, hnr_voc
            )
            if conf_voc > best_result[1]:
                best_result = (gen_voc, conf_voc, f0_voc, idx)

    # 2. Thu tren original_audio neu vocals chua dat do tin cay cao hoac HNR thap
    # (Vi Demucs co the da cat mat tan so giong noi khi tach nhac)
    if secondary_path and (best_result[1] < CONFIDENCE_THRESHOLD or best_result[0] == "unknown"):
        slice_orig = extract_segment_audio_slice(secondary_path, start_s, end_s)
        if slice_orig is not None:
            y_orig, sr_orig = slice_orig
            f0_orig, cnt_orig, conf_orig, gen_orig, hnr_orig = estimate_f0_pitch(y_orig, sr_orig)
            logger.debug(
                "Cau #%d (original): F0=%.1f, count=%d, conf=%.2f, gender=%s, HNR=%.1f",
                idx, f0_orig, cnt_orig, conf_orig, gen_orig, hnr_orig
            )
            if conf_orig > best_result[1]:
                best_result = (gen_orig, conf_orig, f0_orig, idx)

    # 3. Danh gia ket qua tren DUY NHAT cau dau
    gen, conf, f0, _ = best_result
    if conf >= CONFIDENCE_THRESHOLD:
        logger.info(
            "✅ Nhận diện thành công người nói đầu tiên (câu #%d): %s (F0=%.1f Hz, độ tin cậy=%.2f)",
            idx, "NỮ" if gen == "female" else "NAM", f0, conf
        )
        return gen, conf, f0, idx

    logger.info(
        "⚠️ Câu thoại đầu tiên (câu #%d) không đủ độ tin cậy (conf=%.2f, F0=%.1f Hz). "
        "KHÔNG phân tích các câu sau để tránh nhầm người nói tiếp theo. Kích hoạt fallback an toàn.",
        idx, conf, f0
    )
    return "unknown", conf, f0, idx


def find_rvc_model_path(workspace: Optional[str] = None) -> Optional[str]:
    """Tim model RVC Chi Mai (.pth) trong cac thu muc tieu chuan cua he thong."""
    backend_dir = Path(__file__).resolve().parents[1]
    ws = Path(workspace) if workspace else Path(os.getenv("AUTODUB_WORKSPACE", backend_dir.parent / "workspace"))
    search_dirs = [
        backend_dir.parent / "MyVoiceModel_v2",
        ws.parent / "MyVoiceModel_v2",
        ws / "MyVoiceModel_v2",
        ws / "models" / "rvc",
        backend_dir.parent / "models" / "rvc",
    ]
    for d in search_dirs:
        if d.is_dir():
            for f in sorted(os.listdir(d)):
                if f.endswith(".pth"):
                    candidate = d / f
                    try:
                        if candidate.stat().st_size > 1024:
                            return str(candidate.resolve())
                    except OSError:
                        continue
    return None


def resolve_locked_voice(
    gender: str,
    confidence: float,
    median_f0: float,
    first_seg_index: int,
    workspace: Optional[str] = None,
    default_voice_id: Optional[str] = None,
    rvc_model_path: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Quy tac anh xa giong theo ke hoach Codex:
    - Nguoi noi dau la NU -> Khoa Chi Mai (chi-mai, RVC)
    - Nguoi noi dau la NAM -> Khoa Thanh Nien Tu Tin (capcut-BV075_streaming, param: BV075_streaming)
    - Khong ro / nhieu / ngan -> Fallback ve giong mac dinh; ghi log ro rang.
    """
    from ai.voice_cloning import rvc_runtime_available

    # 1. NGUOI NOI DAU LA NU
    if gender == "female" and confidence >= CONFIDENCE_THRESHOLD:
        resolved_rvc = rvc_model_path or find_rvc_model_path(workspace)
        if resolved_rvc and rvc_runtime_available():
            return {
                "voice_id": VOICE_FEMALE_ID,
                "voice_source": "rvc",
                "voice_param": resolved_rvc,
                "voice_label": "Chí Mai · RVC (Khóa theo giọng nữ đầu video)",
                "detected_gender": "female",
                "confidence": confidence,
                "median_f0": median_f0,
                "first_segment_index": first_seg_index,
                "rule": "first_speaker_female_rvc",
            }
        else:
            logger.warning(
                "Phat hien giong nu dau video nhung thieu RVC runtime/model. Dung CapCut Mai lam du phong nu."
            )
            return {
                "voice_id": VOICE_FEMALE_ID,
                "voice_source": "capcut",
                "voice_param": "BV562_streaming",
                "voice_label": "CapCut · Mai (Dự phòng cho Chí Mai khi thiếu RVC)",
                "detected_gender": "female",
                "confidence": confidence,
                "median_f0": median_f0,
                "first_segment_index": first_seg_index,
                "rule": "first_speaker_female_capcut_fallback",
            }

    # 2. NGUOI NOI DAU LA NAM
    if gender == "male" and confidence >= CONFIDENCE_THRESHOLD:
        return {
            "voice_id": VOICE_MALE_ID,
            "voice_source": "capcut",
            "voice_param": VOICE_MALE_PARAM,
            "voice_label": "CapCut · Thanh Niên Tự Tin (Khóa theo giọng nam đầu video)",
            "detected_gender": "male",
            "confidence": confidence,
            "median_f0": median_f0,
            "first_segment_index": first_seg_index,
            "rule": "first_speaker_male_capcut",
        }

    # 3. KHONG XAC DINH RO / NHIEU -> FALLBACK VE GIONG MAC DINH
    v_id = default_voice_id or "chi-mai"
    v_source = "rvc"
    v_param = ""
    v_label = "Chí Mai · RVC (Mặc định)"

    try:
        import voice_selection
        cfg_voice = voice_selection.selected()
        v_id = cfg_voice["id"]
        v_source = cfg_voice["source"]
        v_param = cfg_voice["param"]
        v_label = cfg_voice["label"]

        if v_id == "chi-mai":
            resolved_rvc = rvc_model_path or find_rvc_model_path(workspace)
            if resolved_rvc and rvc_runtime_available():
                v_source = "rvc"
                v_param = resolved_rvc
            else:
                v_source = "capcut"
                v_param = "BV562_streaming"
                v_label = "CapCut · Mai (Dự phòng khi thiếu RVC)"
    except Exception as e:
        logger.debug("Khong the doc voice_selection.selected(): %s", e)
        resolved_rvc = rvc_model_path or find_rvc_model_path(workspace)
        if resolved_rvc and rvc_runtime_available():
            v_id = "chi-mai"
            v_source = "rvc"
            v_param = resolved_rvc
            v_label = "Chí Mai · RVC"
        else:
            v_id = "chi-mai"
            v_source = "capcut"
            v_param = "BV562_streaming"
            v_label = "CapCut · Mai"

    logger.info(
        "Không xác định rõ giới tính người nói đầu (F0=%.1f Hz, conf=%.2f, giới tính=%s). "
        "Khóa giọng mặc định: %s (%s)",
        median_f0, confidence, gender, v_label, v_id
    )

    return {
        "voice_id": v_id,
        "voice_source": v_source,
        "voice_param": v_param,
        "voice_label": f"{v_label} (Mặc định do âm thanh đầu video không rõ)",
        "detected_gender": gender,
        "confidence": confidence,
        "median_f0": median_f0,
        "first_segment_index": first_seg_index,
        "rule": "fallback_default_voice",
    }


def get_locked_voice(out_dir: str | Path) -> Optional[Dict[str, Any]]:
    """Kiem tra va doc voice_lock.json neu da ton tai trong thu muc job."""
    lock_file = Path(out_dir) / VOICE_LOCK_FILENAME
    if lock_file.is_file():
        try:
            data = json.loads(lock_file.read_text(encoding="utf-8"))
            if data.get("voice_id") and data.get("voice_source"):
                return data
        except Exception as e:
            logger.warning("Khong the doc file khoa giong %s: %s", lock_file, e)
    return None


def lock_video_voice(
    out_dir: str | Path,
    srt_segments: Sequence[Any],
    vocals_path: Optional[str | Path] = None,
    original_audio_path: Optional[str | Path] = None,
    workspace: Optional[str] = None,
    rvc_model_path: Optional[str] = None,
    default_voice_id: Optional[str] = None,
    force_reselect: bool = False,
) -> Dict[str, Any]:
    """
    Diem vao duy nhat (Unified Entrypoint):
    Chon va KHOA giong duy nhat cho toan bo video.
    Bao dam nguyen tac:
    - 1 video = 1 giong doc duy nhat tu dau den cuoi.
    - Cac giong xuat hien sau KHONG lam doi giong video.
    - Snapshot persisted tai voice_lock.json de resume/retry giu nguyen 100%.
    """
    out_dir_path = Path(out_dir)
    out_dir_path.mkdir(parents=True, exist_ok=True)
    lock_file = out_dir_path / VOICE_LOCK_FILENAME

    # 1. Neu video da duoc khoa giong tu truoc, tai va dung lai ngay lap tuc!
    if not force_reselect:
        existing = get_locked_voice(out_dir_path)
        if existing:
            logger.info(
                "🔒 Giữ nguyên giọng đã khóa cho video: %s (id=%s, nguồn=%s, rule=%s)",
                existing.get("voice_label"),
                existing.get("voice_id"),
                existing.get("voice_source"),
                existing.get("rule"),
            )
            return existing

    # 2. Nhan dien gioi tinh cua nguoi noi dau tien du tin cay
    gender, confidence, median_f0, seg_idx = detect_first_speaker_gender(
        audio_path=vocals_path,
        srt_segments=srt_segments,
        backup_audio_path=original_audio_path,
    )

    # 3. Anh xa sang giong doc theo quy tac
    locked = resolve_locked_voice(
        gender=gender,
        confidence=confidence,
        median_f0=median_f0,
        first_seg_index=seg_idx,
        workspace=workspace,
        default_voice_id=default_voice_id,
        rvc_model_path=rvc_model_path,
    )
    locked["locked_at"] = datetime.now(timezone.utc).isoformat()
    locked["version"] = "v1_codex_voice_lock_1.0"

    # 4. Luu voice_lock.json an toan (atomic write)
    tmp_file = out_dir_path / f"{VOICE_LOCK_FILENAME}.tmp"
    try:
        tmp_file.write_text(json.dumps(locked, indent=2, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp_file, lock_file)
        logger.info(
            "🔒 Đã khóa giọng thành công cho video vào %s: %s (id=%s)",
            lock_file, locked.get("voice_label"), locked.get("voice_id")
        )
    except Exception as e:
        logger.error("Khong the luu voice_lock.json vao %s: %s", lock_file, e)

    return locked
