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

import hashlib
import json
import logging
import os
import re
import time
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


CONFIG_FILENAME = "v1_auto_voice.json"
PREFERRED_CHI_MAI_MODELS = [
    "mi-giong_cua_toi_v2.pth",
    "chi-mai.pth",
    "chimai.pth",
    "chi_mai.pth",
]


def _resolve_control_dirs(workspace: Optional[str] = None) -> List[Path]:
    """
    Xác định tất cả các thư mục control trong workspace được yêu cầu để đảm bảo đồng bộ 100% (Codex Point 1).
    Nếu workspace có cả workspace/bot_system/control và workspace/control, trả về cả hai để đồng bộ.
    """
    if workspace:
        base_ws = Path(workspace)
    else:
        ws_env = os.getenv("AUTODUB_WORKSPACE")
        base_ws = Path(ws_env) if ws_env else Path(r"C:\tool v1\workspace")

    found_dirs: List[Path] = []
    seen = set()
    for sub in ["bot_system/control", "control"]:
        p = base_ws / sub
        if p.is_dir():
            resolved_str = str(p.resolve())
            if resolved_str not in seen:
                seen.add(resolved_str)
                found_dirs.append(p)

    if not found_dirs:
        # Nếu chưa thư mục nào tồn tại trong workspace này, dùng workspace/control chuẩn
        default_dir = base_ws / "control"
        found_dirs.append(default_dir)

    return found_dirs


def get_auto_voice_config_path(workspace: Optional[str] = None) -> Path:
    """Trả về file config ở thư mục control chính (ưu tiên thư mục đã có file)."""
    dirs = _resolve_control_dirs(workspace)
    for d in dirs:
        p = d / CONFIG_FILENAME
        if p.is_file():
            return p
    return dirs[0] / CONFIG_FILENAME


def get_auto_voice_enabled(workspace: Optional[str] = None) -> bool:
    """Đọc trạng thái Auto Voice từ các thư mục control khả dụng."""
    dirs = _resolve_control_dirs(workspace)
    for d in dirs:
        cfg_path = d / CONFIG_FILENAME
        if cfg_path.is_file():
            try:
                data = json.loads(cfg_path.read_text(encoding="utf-8"))
                return bool(data.get("enabled", True))
            except Exception as e:
                logger.warning("Không thể đọc cấu hình %s: %s", cfg_path, e)
    return True  # Mặc định bật theo khuyến nghị Codex


def set_auto_voice_enabled(enabled: bool, updated_by: str = "system", workspace: Optional[str] = None) -> bool:
    """
    Ghi đồng bộ và nguyên tử trạng thái Auto Voice vào TẤT CẢ các thư mục control hiện hữu (Codex Point 1).
    Xác nhận đọc lại ngay sau khi ghi để bảo đảm tính toàn vẹn (Codex Point 2).
    Trả về True nếu ghi thành công và xác thực hợp lệ; ngược lại trả về False.
    """
    dirs = _resolve_control_dirs(workspace)
    payload = {
        "enabled": bool(enabled),
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "updated_by": str(updated_by)
    }
    payload_json = json.dumps(payload, indent=2, ensure_ascii=False)
    success_count = 0

    for d in dirs:
        cfg_path = d / CONFIG_FILENAME
        tmp = cfg_path.with_suffix(".tmp")
        try:
            d.mkdir(parents=True, exist_ok=True)
            tmp.write_text(payload_json, encoding="utf-8")
            os.replace(tmp, cfg_path)
            # Kiểm tra xác thực đọc lại dữ liệu vừa ghi
            verify_data = json.loads(cfg_path.read_text(encoding="utf-8"))
            if bool(verify_data.get("enabled")) != bool(enabled):
                raise IOError(f"Xác nhận ghi file {cfg_path} thất bại: giá trị không khớp mong muốn")
            success_count += 1
            logger.info("Đã đồng bộ cấu hình Auto Voice: %s (bởi %s) tại %s", "BẬT" if enabled else "TẮT", updated_by, cfg_path)
        except Exception as e:
            logger.error("Lỗi khi lưu cấu hình auto voice tại %s: %s", cfg_path, e)
            try:
                if tmp.exists():
                    tmp.unlink(missing_ok=True)
            except Exception:
                pass
            return False

    return success_count > 0


def get_auto_voice_mode(workspace: Optional[str] = None) -> str:
    return "auto" if get_auto_voice_enabled(workspace) else "manual"


def get_manual_voice_info(workspace: Optional[str] = None) -> Dict[str, Any]:
    try:
        import voice_selection
        return voice_selection.selected()
    except Exception as e:
        logger.warning("Không thể lấy giọng thủ công đang chọn: %s", e)
        return {
            "id": "chi-mai",
            "source": "rvc",
            "param": "chi-mai",
            "label": "Chí Mai (RVC)"
        }



def compute_file_sha256(file_path: Optional[str | Path], max_bytes: int = 16 * 1024 * 1024) -> str:
    """Tính SHA-256 nội dung file nhanh và chính xác làm fingerprint gắn với snapshot."""
    if not file_path:
        return ""
    p = Path(file_path)
    if not p.is_file():
        return ""
    try:
        size = p.stat().st_size
        hasher = hashlib.sha256()
        hasher.update(str(size).encode("utf-8"))
        with open(p, "rb") as f:
            if size <= max_bytes:
                hasher.update(f.read())
            else:
                half = max_bytes // 2
                hasher.update(f.read(half))
                f.seek(max(0, size - half))
                hasher.update(f.read(half))
        return hasher.hexdigest()
    except Exception as e:
        logger.warning("Không thể tính hash file %s: %s", p, e)
        return ""


def find_rvc_model_path(workspace: Optional[str] = None, preferred_name: Optional[str] = None) -> Optional[str]:
    """
    Tìm chính xác model RVC Chí Mai trong các thư mục tiêu chuẩn của hệ thống.
    Ưu tiên các tên file cấu hình Chí Mai trước, không lấy ngẫu nhiên file .pth đầu tiên (Codex Point 1.4).
    """
    backend_dir = Path(__file__).resolve().parents[1]
    ws = Path(workspace) if workspace else Path(os.getenv("AUTODUB_WORKSPACE", backend_dir.parent / "workspace"))
    search_dirs = [
        backend_dir.parent / "MyVoiceModel_v2",
        ws.parent / "MyVoiceModel_v2",
        ws / "MyVoiceModel_v2",
        ws / "models" / "rvc",
        backend_dir.parent / "models" / "rvc",
    ]
    target_names = [preferred_name] if preferred_name else PREFERRED_CHI_MAI_MODELS

    # 1. Tìm chính xác theo danh sách model Chí Mai cấu hình
    for d in search_dirs:
        if d.is_dir():
            for name in target_names:
                candidate = d / name
                try:
                    if candidate.is_file() and candidate.stat().st_size > 1024:
                        return str(candidate.resolve())
                except OSError:
                    continue

    # 2. Nếu không tìm thấy tên chính xác, tìm file .pth hợp lệ trong MyVoiceModel_v2
    for d in search_dirs:
        if d.is_dir() and "MyVoiceModel" in d.name:
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
    voice_mode: str = "auto",
) -> Dict[str, Any]:
    """
    Quy tắc quyết định giọng (Unified Decision Matrix):
    - Chế độ MANUAL: Dùng trực tiếp giọng thủ công đang chọn, không nhận diện.
    - Chế độ AUTO:
      + Câu thoại đầu là NỮ -> Bắt buộc dùng Chí Mai RVC. Nếu thiếu model/runtime, BÁO LỖI rõ ràng trước TTS.
      + Câu thoại đầu là NAM -> Thanh Niên Tự Tin BV075_streaming.
      + Câu đầu không chắc (conf < 0.70) -> Fallback về giọng thủ công đang chọn, ghi log rõ lý do.
    """
    from ai.voice_cloning import rvc_runtime_available

    # 1. CHẾ ĐỘ THỦ CÔNG (MANUAL MODE)
    if voice_mode == "manual":
        import voice_selection
        cfg_voice = voice_selection.selected()
        v_id = cfg_voice["id"]
        v_source = cfg_voice["source"]
        v_param = cfg_voice["param"]
        v_label = cfg_voice["label"]

        if v_id == "chi-mai":
            resolved_rvc = rvc_model_path or find_rvc_model_path(workspace)
            if not resolved_rvc or not rvc_runtime_available():
                raise RuntimeError("Giọng thủ công đang chọn là 'chi-mai' nhưng thiếu RVC model/runtime khả dụng trên hệ thống. Dừng trước TTS.")
            v_param = resolved_rvc

        logger.info("Chế độ Manual: Dùng trực tiếp giọng thủ công đã chọn: %s (%s). Bỏ qua bước nhận diện F0/HNR.", v_label, v_id)
        return {
            "voice_id": v_id,
            "voice_source": v_source,
            "voice_param": str(v_param),
            "voice_label": f"{v_label} (Chế độ thủ công)",
            "detected_gender": "manual",
            "confidence": 1.0,
            "median_f0": 0.0,
            "first_segment_index": 0,
            "rule": "manual_selection",
            "voice_mode": "manual",
        }

    # 2. CHẾ ĐỘ TỰ ĐỘNG (AUTO MODE) - NGUOI NOI DAU LA NU
    if gender == "female" and confidence >= CONFIDENCE_THRESHOLD:
        resolved_rvc = rvc_model_path or find_rvc_model_path(workspace)
        if not resolved_rvc:
            raise RuntimeError(
                "Chế độ Auto Voice: Phát hiện người nói đầu là NỮ nhưng không tìm thấy file model RVC Chí Mai ('mi-giong_cua_toi_v2.pth'). "
                "Không âm thầm thay thế bằng CapCut Mai để bảo đảm độ thuần giọng. "
                "Hãy kiểm tra thư mục MyVoiceModel_v2 hoặc tắt auto voice (/voice_auto off)."
            )
        if not rvc_runtime_available():
            raise RuntimeError(
                "Chế độ Auto Voice: Phát hiện người nói đầu là NỮ nhưng RVC runtime hoặc CUDA không khả dụng trên hệ thống. "
                "Dừng job trước khi tạo TTS."
            )
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
            "voice_mode": "auto",
        }

    # 3. CHẾ ĐỘ TỰ ĐỘNG - NGUOI NOI DAU LA NAM
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
            "voice_mode": "auto",
        }

    # 4. CHẾ ĐỘ TỰ ĐỘNG - CÂU ĐẦU KHÔNG XÁC ĐỊNH CHẮC (UNKNOWN) -> FALLBACK VỀ GIỌNG THỦ CÔNG
    import voice_selection
    cfg_voice = voice_selection.selected()
    v_id = cfg_voice["id"]
    v_source = cfg_voice["source"]
    v_param = cfg_voice["param"]
    v_label = cfg_voice["label"]

    if v_id == "chi-mai":
        resolved_rvc = rvc_model_path or find_rvc_model_path(workspace)
        if not resolved_rvc or not rvc_runtime_available():
            raise RuntimeError("Fallback về giọng thủ công 'chi-mai' nhưng thiếu RVC model/runtime. Dừng trước TTS.")
        v_param = resolved_rvc

    logger.info(
        "Chế độ Auto Voice: Câu đầu không xác định chắc giới tính (conf=%.2f < %.2f, F0=%.1f Hz, gender=%s). "
        "Lý do fallback: Âm thanh câu đầu mờ/ồn hoặc không đủ điều kiện phân loại cao độ. "
        "Chuyển sang giọng thủ công đang chọn: %s (%s).",
        confidence, CONFIDENCE_THRESHOLD, median_f0, gender, v_label, v_id
    )

    return {
        "voice_id": v_id,
        "voice_source": v_source,
        "voice_param": str(v_param),
        "voice_label": f"{v_label} (Fallback do câu đầu không xác định chắc giới tính)",
        "detected_gender": gender,
        "confidence": confidence,
        "median_f0": median_f0,
        "first_segment_index": first_seg_index,
        "rule": "fallback_manual_choice_unknown_first_speaker",
        "voice_mode": "auto",
    }


def get_locked_voice(
    out_dir: str | Path,
    expected_video_hash: Optional[str] = None,
    expected_mode: Optional[str] = None
) -> Optional[Dict[str, Any]]:
    """Kiem tra va doc voice_lock.json neu da ton tai trong thu muc job va hop le."""
    lock_file = Path(out_dir) / VOICE_LOCK_FILENAME
    if lock_file.is_file():
        try:
            data = json.loads(lock_file.read_text(encoding="utf-8"))
            required = ["voice_id", "voice_source", "voice_param"]
            if not all(data.get(k) for k in required):
                logger.warning("File snapshot khóa giọng %s thiếu trường bắt buộc.", lock_file)
                return None
            data.setdefault("rule_version", "v1_codex_voice_snapshot_1.0")

            if expected_video_hash and data.get("video_hash"):
                if data["video_hash"] != expected_video_hash:
                    logger.info("Video hash không khớp (cũ=%s, mới=%s); bỏ qua snapshot cũ.", data["video_hash"][:8], expected_video_hash[:8])
                    return None

            if expected_mode and data.get("voice_mode"):
                if data["voice_mode"] != expected_mode:
                    logger.info("Chế độ giọng đổi từ %s sang %s; bỏ qua snapshot cũ.", data["voice_mode"], expected_mode)
                    return None

            return data
        except Exception as e:
            logger.warning("Không thể đọc file khóa giọng %s: %s", lock_file, e)
    return None


def decide_video_voice(
    out_dir: str | Path,
    srt_segments: Sequence[Any],
    vocals_path: Optional[str | Path] = None,
    original_audio_path: Optional[str | Path] = None,
    video_path: Optional[str | Path] = None,
    voice_mode: Optional[str] = None,
    workspace: Optional[str] = None,
    rvc_model_path: Optional[str] = None,
    default_voice_id: Optional[str] = None,
    force_reselect: bool = False,
    job_id: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Hàm quyết định giọng chung (Unified Voice Decision Function - Codex Plan):
    - Chế độ Auto: Phân tích câu thoại đầu -> Chọn và lưu snapshot (Nữ -> Chí Mai RVC; Nam -> BV075; Unknown -> thủ công).
    - Chế độ Manual: Lấy giọng đã chọn thủ công -> Lưu snapshot (Thời gian phân tích = 0s, không chạy pitch).
    - Snapshot gắn với hash nội dung video và chế độ.
    - Nếu ghi snapshot thất bại, dừng trước TTS (fail-closed).
    """
    out_dir_path = Path(out_dir)
    out_dir_path.mkdir(parents=True, exist_ok=True)
    lock_file = out_dir_path / VOICE_LOCK_FILENAME

    actual_mode = voice_mode or get_auto_voice_mode(workspace)
    resolved_job_id = job_id or out_dir_path.name
    content_hash = compute_file_sha256(video_path or original_audio_path)

    # 1. Kiểm tra snapshot đã tồn tại và hợp lệ
    if not force_reselect:
        existing = get_locked_voice(
            out_dir_path,
            expected_video_hash=content_hash if content_hash else None,
            expected_mode=actual_mode
        )
        if existing:
            logger.info(
                "🔒 Giữ nguyên quyết định giọng đã khóa cho video: %s (id=%s, nguồn=%s, mode=%s, rule=%s)",
                existing.get("voice_label"),
                existing.get("voice_id"),
                existing.get("voice_source"),
                existing.get("voice_mode"),
                existing.get("rule"),
            )
            return existing

    # 2. Xử lý theo từng chế độ
    pitch_elapsed = 0.0
    if actual_mode == "manual":
        # MANUAL: Không nạp librosa, không tính cao độ, thời gian = 0.0s
        locked = resolve_locked_voice(
            gender="manual",
            confidence=1.0,
            median_f0=0.0,
            first_seg_index=0,
            workspace=workspace,
            default_voice_id=default_voice_id,
            rvc_model_path=rvc_model_path,
            voice_mode="manual",
        )
    else:
        # AUTO: Phân tích âm học duy nhất câu thoại đầu tiên
        t0 = time.perf_counter()
        gender, confidence, median_f0, seg_idx = detect_first_speaker_gender(
            audio_path=vocals_path,
            srt_segments=srt_segments,
            backup_audio_path=original_audio_path,
        )
        pitch_elapsed = time.perf_counter() - t0
        logger.info(
            "⏱️ Thời gian phân tích cao độ giọng câu đầu: %.3f giây (gender=%s, conf=%.2f, F0=%.1f)",
            pitch_elapsed, gender, confidence, median_f0
        )

        locked = resolve_locked_voice(
            gender=gender,
            confidence=confidence,
            median_f0=median_f0,
            first_seg_index=seg_idx,
            workspace=workspace,
            default_voice_id=default_voice_id,
            rvc_model_path=rvc_model_path,
            voice_mode="auto",
        )

    # 3. Gắn metadata snapshot đầy đủ
    locked["job_id"] = resolved_job_id
    locked["video_hash"] = content_hash
    locked["voice_mode"] = actual_mode
    locked["analysis_time_sec"] = round(pitch_elapsed, 4)
    locked["rule_version"] = "v1_codex_voice_snapshot_2.0"
    locked["locked_at"] = datetime.now(timezone.utc).isoformat()

    # 4. Ghi snapshot nguyên tử (atomic write). Nếu thất bại, DỪNG TRƯỚC TTS (Codex Requirement)
    tmp_file = out_dir_path / f"{VOICE_LOCK_FILENAME}.tmp"
    try:
        tmp_file.write_text(json.dumps(locked, indent=2, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp_file, lock_file)
        logger.info(
            "🔒 Đã ghi snapshot quyết định giọng thành công vào %s: %s (id=%s, mode=%s)",
            lock_file, locked.get("voice_label"), locked.get("voice_id"), actual_mode
        )
    except Exception as e:
        logger.error("Không thể ghi snapshot vào %s: %s", lock_file, e)
        raise RuntimeError(f"Lỗi nghiêm trọng: Không thể lưu snapshot quyết định giọng vào {lock_file}: {e}")

    return locked


def lock_video_voice(
    out_dir: str | Path,
    srt_segments: Sequence[Any],
    vocals_path: Optional[str | Path] = None,
    original_audio_path: Optional[str | Path] = None,
    workspace: Optional[str] = None,
    rvc_model_path: Optional[str] = None,
    default_voice_id: Optional[str] = None,
    force_reselect: bool = False,
    video_path: Optional[str | Path] = None,
    voice_mode: Optional[str] = None,
) -> Dict[str, Any]:
    """Wrapper tương thích ngược gọi decide_video_voice."""
    return decide_video_voice(
        out_dir=out_dir,
        srt_segments=srt_segments,
        vocals_path=vocals_path,
        original_audio_path=original_audio_path,
        video_path=video_path,
        voice_mode=voice_mode,
        workspace=workspace,
        rvc_model_path=rvc_model_path,
        default_voice_id=default_voice_id,
        force_reselect=force_reselect,
    )
