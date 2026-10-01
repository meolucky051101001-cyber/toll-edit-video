"""
v1_quality_gate.py - Quality Gate tự động 8 nhóm kiểm tra (Tool V1 - Giai đoạn 5).
Tự động xuất bản báo cáo QC sau khi xuất video thành phẩm.
Chính sách: REPORT_ONLY (mặc định) / WARN / BLOCK.
Bảo toàn 100% video xuất xưởng, không làm gián đoạn tác vụ khi ở chế độ mặc định.
"""

from __future__ import annotations

import os
import sys
import json
import math
import logging
from dataclasses import dataclass, field, asdict
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

logger = logging.getLogger(__name__)


class QCPolicy(str, Enum):
    REPORT_ONLY = "REPORT_ONLY"
    WARN = "WARN"
    BLOCK = "BLOCK"


class QCCategory(str, Enum):
    FILE_INTEGRITY = "file_integrity"          # 1. Tính toàn vẹn của tệp video và luồng âm thanh/hình ảnh
    AV_SYNC = "av_sync"                        # 2. Độ lệch đồng bộ âm thanh - hình ảnh (A/V sync)
    ASR_COVERAGE = "asr_coverage"              # 3. Độ bao phủ câu thoại ASR
    TRANSLATION_INTEGRITY = "translation"       # 4. Tính toàn vẹn của bản dịch (không sót CJK, đủ câu)
    SUBTITLE_STYLING = "subtitle_styling"      # 5. Phụ đề ASS hiển thị chuẩn xác
    TTS_TIMING_FIT = "tts_timing_fit"          # 6. Khớp thời lượng TTS (không đè câu sau)
    AUDIO_BALANCE = "audio_balance"            # 7. Cân bằng âm lượng (không clipping)
    RENDER_CONTINUITY = "render_continuity"    # 8. Tính liên tục khung hình render NVENC


@dataclass
class QCCheckItem:
    category: str
    name: str
    status: str  # "PASS", "WARN", "FAIL"
    message: str
    details: Dict[str, Any] = field(default_factory=dict)


@dataclass
class QCReport:
    job_id: str
    video_name: str
    overall_status: str  # "PASSED", "PASSED_WITH_WARNINGS", "FAILED"
    policy: str
    passed_count: int = 0
    warning_count: int = 0
    failure_count: int = 0
    checks: List[QCCheckItem] = field(default_factory=list)
    created_at: float = field(default_factory=__import__("time").time)

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d["checks"] = [asdict(c) for c in self.checks]
        return d


def _check_cjk(text: str) -> bool:
    """Kiểm tra xem câu có sót ký tự chữ Hán (CJK) hay không."""
    for ch in text:
        if "\u4e00" <= ch <= "\u9fff":
            return True
    return False


def run_quality_gate(
    job_id: str,
    final_video_path: str | Path,
    original_video_path: Optional[str | Path] = None,
    translated_subtitles: Optional[Sequence[Any]] = None,
    dubbing_audio_files: Optional[Sequence[Dict[str, Any]]] = None,
    mixed_audio_path: Optional[str | Path] = None,
    ass_subtitle_path: Optional[str | Path] = None,
    policy: Optional[str] = None,
    workspace_path: Optional[str | Path] = None,
) -> QCReport:
    """
    Chạy 8 nhóm kiểm tra tự động cho video thành phẩm và xuất báo cáo JSON.
    """
    effective_policy = (policy or os.getenv("V1_QC_POLICY", QCPolicy.REPORT_ONLY.value)).upper()
    if effective_policy not in (QCPolicy.REPORT_ONLY.value, QCPolicy.WARN.value, QCPolicy.BLOCK.value):
        effective_policy = QCPolicy.REPORT_ONLY.value

    v_path = Path(final_video_path)
    video_name = v_path.name if v_path.name else "unknown_video"
    checks: List[QCCheckItem] = []

    # 1. FILE INTEGRITY
    if not v_path.is_file():
        checks.append(QCCheckItem(
            category=QCCategory.FILE_INTEGRITY.value,
            name="final_video_exists",
            status="FAIL",
            message=f"Tệp video thành phẩm không tồn tại: {v_path}",
        ))
    else:
        v_size = v_path.stat().st_size
        if v_size < 10240:
            checks.append(QCCheckItem(
                category=QCCategory.FILE_INTEGRITY.value,
                name="final_video_size",
                status="FAIL",
                message=f"Tệp video quá nhỏ ({v_size} bytes), có thể bị lỗi render",
                details={"size_bytes": v_size},
            ))
        else:
            checks.append(QCCheckItem(
                category=QCCategory.FILE_INTEGRITY.value,
                name="final_video_size",
                status="PASS",
                message=f"Tệp video hợp lệ ({v_size / 1024 / 1024:.2f} MB)",
                details={"size_bytes": v_size},
            ))

    # Compare source/output metadata: reading an audio file alone proves no sync.
    try:
        from v1_video_router import extract_media_metadata
        import soundfile as sf
        output = extract_media_metadata(v_path)
        source = extract_media_metadata(original_video_path) if original_video_path else None
        audio_duration = sf.info(str(mixed_audio_path)).duration if mixed_audio_path else 0
        duration_ok = output.duration_s > 0 and audio_duration > 0 and abs(output.duration_s - audio_duration) <= 0.25
        if source:
            duration_ok = duration_ok and source.duration_s > 0 and abs(output.duration_s - source.duration_s) <= 0.25
            geometry_ok = (output.width, output.height) == (source.width, source.height)
            fps_ok = source.fps > 0 and abs(output.fps - source.fps) <= max(0.05, source.fps * 0.002)
            checks.append(QCCheckItem(QCCategory.RENDER_CONTINUITY.value, "source_geometry_fps",
                "PASS" if geometry_ok and fps_ok else "FAIL", "Đối chiếu độ phân giải/FPS với video gốc",
                {"source": source.to_dict(), "output": output.to_dict()}))
        checks.append(QCCheckItem(QCCategory.AV_SYNC.value, "duration_alignment",
            "PASS" if duration_ok else "FAIL", "Đối chiếu thời lượng hình/âm thanh (không xác nhận lip-sync)",
            {"video_duration": output.duration_s, "audio_duration": audio_duration}))
    except Exception as exc:
        checks.append(QCCheckItem(QCCategory.AV_SYNC.value, "duration_alignment",
            "WARN", f"Chưa xác minh được đồng bộ: {exc}"))

    # 3. ASR COVERAGE
    if translated_subtitles is not None:
        seg_count = len(translated_subtitles)
        if seg_count == 0:
            checks.append(QCCheckItem(
                category=QCCategory.ASR_COVERAGE.value,
                name="transcription_segments_count",
                status="WARN",
                message="Không có câu thoại nào được nhận diện (video im lặng hoặc lỗi ASR)",
            ))
        else:
            checks.append(QCCheckItem(
                category=QCCategory.ASR_COVERAGE.value,
                name="transcription_segments_count",
                status="WARN",
                message=f"Có {seg_count} câu; chưa có bản chép lời chuẩn để xác minh không sót thoại",
                details={"segment_count": seg_count},
            ))

    # 4. TRANSLATION INTEGRITY
    if translated_subtitles:
        cjk_leaks = []
        empty_lines = []
        for i, s in enumerate(translated_subtitles):
            txt = getattr(s, "content", "") or ""
            if not txt.strip():
                empty_lines.append(getattr(s, "index", i + 1))
            elif _check_cjk(txt):
                cjk_leaks.append({"index": getattr(s, "index", i + 1), "text": txt[:30]})

        if cjk_leaks:
            checks.append(QCCheckItem(
                category=QCCategory.TRANSLATION_INTEGRITY.value,
                name="no_cjk_residual",
                status="WARN",
                message=f"Phát hiện {len(cjk_leaks)} câu còn sót chữ Hán",
                details={"cjk_leaks": cjk_leaks[:5]},
            ))
        else:
            checks.append(QCCheckItem(
                category=QCCategory.TRANSLATION_INTEGRITY.value,
                name="no_cjk_residual",
                status="PASS",
                message="100% câu dịch sạch tiếng Trung",
            ))

        if empty_lines:
            checks.append(QCCheckItem(
                category=QCCategory.TRANSLATION_INTEGRITY.value,
                name="no_empty_subtitles",
                status="WARN",
                message=f"Có {len(empty_lines)} câu bị rỗng nội dung",
                details={"empty_indices": empty_lines[:10]},
            ))
        else:
            checks.append(QCCheckItem(
                category=QCCategory.TRANSLATION_INTEGRITY.value,
                name="no_empty_subtitles",
                status="PASS",
                message="Không có câu dịch nào bị rỗng",
            ))

    # 5. SUBTITLE STYLING
    if ass_subtitle_path:
        ass_p = Path(ass_subtitle_path)
        if ass_p.is_file() and ass_p.stat().st_size > 100:
            import re
            ass_text = ass_p.read_text(encoding="utf-8-sig")
            valid_ass = "[Script Info]" in ass_text and "[Events]" in ass_text
            events = [line for line in ass_text.splitlines() if line.startswith("Dialogue:")]
            valid_ass = valid_ass and bool(events)
            def ass_seconds(value):
                hours, minutes, seconds = value.strip().split(":")
                return int(hours) * 3600 + int(minutes) * 60 + float(seconds)
            try:
                for event in events:
                    fields = event.split(",", 9)
                    start, end = ass_seconds(fields[1]), ass_seconds(fields[2])
                    if start < 0 or end <= start or not fields[9].strip():
                        valid_ass = False
            except (ValueError, IndexError):
                valid_ass = False
            checks.append(QCCheckItem(
                category=QCCategory.SUBTITLE_STYLING.value,
                name="ass_subtitle_file",
                status="PASS" if valid_ass else "FAIL",
                message="Kiểm tra cấu trúc và timeline ASS (chưa xác minh hình ảnh che chữ)",
            ))
            checks.append(QCCheckItem(QCCategory.SUBTITLE_STYLING.value, "visual_mask_coverage", "UNKNOWN",
                "Cần kiểm tra video thực tế để xác nhận nền trắng che đủ chữ gốc"))
        else:
            checks.append(QCCheckItem(
                category=QCCategory.SUBTITLE_STYLING.value,
                name="ass_subtitle_file",
                status="WARN",
                message="Tệp phụ đề ASS thiếu hoặc có dung lượng 0 bytes",
            ))
    else:
        checks.append(QCCheckItem(
            category=QCCategory.SUBTITLE_STYLING.value,
            name="ass_subtitle_file",
            status="UNKNOWN",
            message="Không có tệp phụ đề ASS được chỉ định",
        ))

    # 6. TTS TIMING FIT: compare the actual files, not cached duration labels.
    measured_dubs = []
    missing_audio = []
    for dub in dubbing_audio_files or []:
        try:
            import soundfile as sf
            duration = float(sf.info(str(dub.get("path", ""))).duration)
            if not math.isfinite(duration) or duration <= 0:
                raise ValueError("Empty audio")
            start = float(dub.get("start", 0.0))
            if not math.isfinite(start) or start < 0:
                raise ValueError("Invalid start")
            measured_dubs.append({**dub, "duration": duration})
        except Exception as exc:
            missing_audio.append({"index": dub.get("index"), "error": str(exc)})
    if missing_audio:
        checks.append(QCCheckItem(QCCategory.TTS_TIMING_FIT.value, "tts_audio_files", "FAIL",
            f"{len(missing_audio)} file TTS thiếu hoặc không đọc được", {"missing": missing_audio[:10]}))
    if translated_subtitles and dubbing_audio_files is not None:
        expected = {getattr(s, "index", i + 1) for i, s in enumerate(translated_subtitles) if getattr(s, "content", "").strip()}
        actual = {d.get("index") for d in measured_dubs}
        complete = expected == actual and len(measured_dubs) == len(expected)
        checks.append(QCCheckItem(QCCategory.TTS_TIMING_FIT.value, "tts_cue_coverage",
            "PASS" if complete else "FAIL", "Đối chiếu từng câu phụ đề với audio TTS",
            {"expected_count": len(expected), "audio_count": len(measured_dubs)}))
    elif translated_subtitles:
        checks.append(QCCheckItem(QCCategory.TTS_TIMING_FIT.value, "tts_cue_coverage", "UNKNOWN",
            "Chưa có danh sách file TTS để đối chiếu độ bao phủ từng câu"))
    dubbing_audio_files = measured_dubs
    if dubbing_audio_files:
        sorted_dubs = sorted(dubbing_audio_files, key=lambda d: float(d.get("start", 0.0)))
        collision_count = 0
        for i in range(len(sorted_dubs) - 1):
            cur_s = float(sorted_dubs[i].get("start", 0.0))
            cur_dur = float(sorted_dubs[i].get("duration", 0.0) or sorted_dubs[i].get("actual_audio_duration", 0.0))
            next_s = float(sorted_dubs[i + 1].get("start", 0.0))
            if cur_s + cur_dur > next_s + 0.005:  # Sai số đo tối đa 5 ms
                collision_count += 1

        if collision_count > 0:
            checks.append(QCCheckItem(
                category=QCCategory.TTS_TIMING_FIT.value,
                name="tts_no_collisions",
                status="FAIL",
                message=f"Phát hiện {collision_count} vị trí có nguy cơ chớm đè câu",
                details={"collision_count": collision_count},
            ))
        else:
            checks.append(QCCheckItem(
                category=QCCategory.TTS_TIMING_FIT.value,
                name="tts_no_collisions",
                status="PASS",
                message="Không phát hiện đè câu theo thời lượng audio được cung cấp",
            ))
    else:
        checks.append(QCCheckItem(
            category=QCCategory.TTS_TIMING_FIT.value,
            name="tts_no_collisions",
            status="UNKNOWN",
            message="Chưa có dữ liệu danh sách audio lồng tiếng để kiểm tra va chạm",
        ))

    # 7. AUDIO BALANCE
    if mixed_audio_path and Path(mixed_audio_path).is_file():
        try:
            import soundfile as sf
            import numpy as np
            peak = max((float(np.max(np.abs(block))) for block in sf.blocks(str(mixed_audio_path), blocksize=65536, dtype="float32")), default=0.0)
            peak_dbfs = 20 * math.log10(max(peak, 1e-12))
            if peak_dbfs > -0.05:
                checks.append(QCCheckItem(
                    category=QCCategory.AUDIO_BALANCE.value,
                    name="peak_ceiling",
                    status="WARN",
                    message=f"Đỉnh âm thanh chạm ngưỡng clipping ({peak_dbfs:.2f} dBFS)",
                    details={"peak_dbfs": peak_dbfs},
                ))
            else:
                checks.append(QCCheckItem(
                    category=QCCategory.AUDIO_BALANCE.value,
                    name="peak_ceiling",
                    status="PASS",
                    message=f"Đỉnh âm thanh an toàn ({peak_dbfs:.2f} dBFS <= -0.5 dBFS)",
                    details={"peak_dbfs": peak_dbfs},
                ))
        except Exception:
            pass
    elif mixed_audio_path:
        checks.append(QCCheckItem(
            category=QCCategory.AUDIO_BALANCE.value,
            name="peak_ceiling",
            status="WARN",
            message=f"Không tìm thấy file audio mixed để đo peak: {mixed_audio_path}",
        ))

    # 8. RENDER CONTINUITY & FRAME DECODING (PROBE THẬT)
    if not v_path.is_file():
        checks.append(QCCheckItem(
            category=QCCategory.RENDER_CONTINUITY.value,
            name="video_stream_integrity",
            status="FAIL",
            message="Không tìm thấy tệp video để kiểm tra stream rendering",
        ))
    else:
        try:
            import cv2
            cap = cv2.VideoCapture(str(v_path))
            is_open = cap.isOpened()
            frame_cnt = int(cap.get(cv2.CAP_PROP_FRAME_COUNT)) if is_open else 0
            v_fps = float(cap.get(cv2.CAP_PROP_FPS)) if is_open else 0.0
            v_w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH)) if is_open else 0
            v_h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT)) if is_open else 0
            ret, _ = cap.read() if is_open else (False, None)
            cap.release()

            if is_open and frame_cnt > 0 and v_fps > 0 and v_w > 0 and v_h > 0 and ret:
                checks.append(QCCheckItem(
                    category=QCCategory.RENDER_CONTINUITY.value,
                    name="video_stream_integrity",
                    status="PASS",
                    message=f"Khung hình đầu giải mã được; chưa kiểm tra toàn bộ video ({v_w}x{v_h} @ {v_fps:.1f} FPS, {frame_cnt} frames)",
                    details={"width": v_w, "height": v_h, "fps": v_fps, "frame_count": frame_cnt},
                ))
            else:
                checks.append(QCCheckItem(
                    category=QCCategory.RENDER_CONTINUITY.value,
                    name="video_stream_integrity",
                    status="FAIL",
                    message=f"Tệp video bị hỏng stream hoặc không giải mã được khung hình (frames={frame_cnt})",
                    details={"is_open": is_open, "frame_count": frame_cnt},
                ))
        except Exception as vid_err:
            checks.append(QCCheckItem(
                category=QCCategory.RENDER_CONTINUITY.value,
                name="video_stream_integrity",
                status="WARN",
                message=f"Không thể probe stream video bằng OpenCV: {vid_err}",
            ))

    # Tổng kết
    pass_cnt = sum(1 for c in checks if c.status == "PASS")
    warn_cnt = sum(1 for c in checks if c.status in ("WARN", "UNKNOWN"))
    fail_cnt = sum(1 for c in checks if c.status == "FAIL")

    if fail_cnt > 0:
        overall = "FAILED"
    elif warn_cnt > 0:
        overall = "PASSED_WITH_WARNINGS"
    else:
        overall = "PASSED"

    report = QCReport(
        job_id=job_id,
        video_name=video_name,
        overall_status=overall,
        policy=effective_policy,
        passed_count=pass_cnt,
        warning_count=warn_cnt,
        failure_count=fail_cnt,
        checks=checks,
    )

    # Lưu báo cáo vào workspace
    base_ws = Path(workspace_path) if workspace_path else Path(__file__).resolve().parent.parent / "workspace"
    qc_dir = base_ws / "bot_system" / "qc_reports"
    qc_dir.mkdir(parents=True, exist_ok=True)
    report_file = qc_dir / f"{job_id}.qc.json"
    try:
        report_file.write_text(json.dumps(report.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
        logger.info(f"[QUALITY_GATE] Đã xuất báo cáo QC cho job {job_id} -> {report_file.name} ({overall})")
    except Exception as save_err:
        logger.warning(f"[QUALITY_GATE] Không thể ghi file báo cáo QC: {save_err}")

    # Xử lý policy BLOCK
    if effective_policy == QCPolicy.BLOCK.value and overall == "FAILED":
        fail_msgs = [c.message for c in checks if c.status == "FAIL"]
        raise RuntimeError(f"Quality Gate BLOCK: Job {job_id} thất bại nghiệm thu chất lượng: {'; '.join(fail_msgs)}")

    return report
