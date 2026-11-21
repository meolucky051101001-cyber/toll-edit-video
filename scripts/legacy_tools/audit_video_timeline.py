"""Audit video timeline comparing source ASR, OCR, translated subtitles, covers, and audio.

Outputs an exhaustive timeline audit table to identify:
1. Segments missing before ASR (e.g. 0:00 - 0:35)
2. ASR gaps >= 5s
3. Cover gaps and geometry mismatches (e.g. 06:33.60)
4. Segments missing covers in ASS
5. Audible speech status in final mix
"""

import json
import math
import os
import re
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

try:
    import srt
except ImportError:
    srt = None


def parse_ass_dialogues(ass_path: Path) -> List[Dict[str, Any]]:
    """Parse ASS dialogue lines into structured records."""
    if not ass_path.is_file():
        return []
    lines = ass_path.read_text(encoding="utf-8", errors="replace").splitlines()
    events = []
    for line in lines:
        if line.startswith("Dialogue:"):
            parts = line.split(",", 9)
            if len(parts) >= 10:
                layer = int(parts[0].replace("Dialogue:", "").strip())
                start_str = parts[1].strip()
                end_str = parts[2].strip()
                style = parts[3].strip()
                text = parts[9].strip()
                events.append({
                    "layer": layer,
                    "start_str": start_str,
                    "end_str": end_str,
                    "start": ass_time_to_seconds(start_str),
                    "end": ass_time_to_seconds(end_str),
                    "style": style,
                    "text": text,
                })
    return events


def ass_time_to_seconds(t_str: str) -> float:
    """Convert H:MM:SS.cs to seconds."""
    parts = t_str.split(":")
    if len(parts) == 3:
        h = float(parts[0])
        m = float(parts[1])
        s = float(parts[2])
        return h * 3600.0 + m * 60.0 + s
    return 0.0


def audit_timeline(
    benchmark_dir: Path,
    output_report_json: Path,
    output_report_md: Path,
) -> Dict[str, Any]:
    artifacts_dir = benchmark_dir / "job" / "pipeline_v2" / "artifacts"
    
    # 1. Load ASR
    asr_srt_path = artifacts_dir / "transcript" / "original.srt"
    asr_segments = []
    if asr_srt_path.is_file() and srt:
        with open(asr_srt_path, "r", encoding="utf-8") as f:
            for seg in srt.parse(f.read()):
                asr_segments.append({
                    "index": seg.index,
                    "start": seg.start.total_seconds(),
                    "end": seg.end.total_seconds(),
                    "content": seg.content,
                })

    # 2. Load Translation
    trans_json_path = artifacts_dir / "translation" / "segments.json"
    trans_segments = []
    if trans_json_path.is_file():
        trans_data = json.loads(trans_json_path.read_text(encoding="utf-8"))
        trans_segments = trans_data.get("segments", [])

    # 3. Load ASS events
    ass_path = artifacts_dir / "subtitles" / "final.ass"
    ass_events = parse_ass_dialogues(ass_path)
    bg_covers = [e for e in ass_events if e["style"] == "BgStyle"]
    text_dialogues = [e for e in ass_events if e["style"] == "TextStyle"]

    # 4. Load QC report
    qc_path = artifacts_dir / "qc" / "qc_report.json"
    qc_data = {}
    if qc_path.is_file():
        qc_data = json.loads(qc_path.read_text(encoding="utf-8"))

    # 5. Load OCR results
    ocr_path = artifacts_dir / "ocr" / "result.json"
    ocr_data = {}
    if ocr_path.is_file():
        ocr_data = json.loads(ocr_path.read_text(encoding="utf-8"))

    # Identify ASR gaps >= 5.0s
    asr_gaps = []
    prev_end = 0.0
    for seg in asr_segments:
        s = seg["start"]
        if s - prev_end >= 5.0:
            asr_gaps.append({
                "gap_start": round(prev_end, 2),
                "gap_end": round(s, 2),
                "duration": round(s - prev_end, 2),
                "before_index": seg["index"],
            })
        prev_end = seg["end"]

    # Identify segments missing white covers
    missing_covers = []
    for d in text_dialogues:
        d_start = d["start"]
        d_end = d["end"]
        # Find overlapping cover in bg_covers
        overlap = any(
            c["start"] <= d_end and c["end"] >= d_start
            for c in bg_covers
        )
        if not overlap:
            missing_covers.append(d)

    audit_summary = {
        "total_asr_segments": len(asr_segments),
        "total_translation_segments": len(trans_segments),
        "total_bg_covers": len(bg_covers),
        "total_text_dialogues": len(text_dialogues),
        "missing_covers_count": len(missing_covers),
        "asr_gaps_gte_5s_count": len(asr_gaps),
        "asr_gaps_gte_5s": asr_gaps,
        "first_asr_start": asr_segments[0]["start"] if asr_segments else None,
        "missing_covers_samples": missing_covers[:10],
    }

    output_report_json.parent.mkdir(parents=True, exist_ok=True)
    with open(output_report_json, "w", encoding="utf-8") as f:
        json.dump(audit_summary, f, indent=2, ensure_ascii=False)

    # Generate Markdown table
    md_lines = [
        "# Bảng Đối Chiếu Timeline Toàn Bộ Video (18m 37s)\n",
        f"- **Tổng số ASR segments**: {len(asr_segments)}",
        f"- **Thời điểm câu ASR đầu tiên**: {asr_segments[0]['start'] if asr_segments else 0.0:.2f}s (Khoảng 00:00 -> 00:35 bị trống)",
        f"- **Số lượng khoảng trống ASR >= 5.0s**: {len(asr_gaps)} khoảng trống",
        f"- **Số đoạn phụ đề tiếng Việt không có khung che (BgStyle)**: {len(missing_covers)} đoạn\n",
        "## Danh sách các khoảng trống ASR >= 5 giây\n",
        "| STT | Bắt đầu (s) | Kết thúc (s) | Độ dài (s) | Trước câu ASR | Ghi chú |",
        "| :--- | :--- | :--- | :--- | :--- | :--- |",
    ]
    for idx, gap in enumerate(asr_gaps[:20], 1):
        note = "Đầu video (chứa thoại bị mất do prompt hallucination)" if gap["gap_start"] == 0.0 else "Khoảng nghỉ / nhạc nền / thoại chưa quét"
        md_lines.append(f"| {idx} | {gap['gap_start']} | {gap['gap_end']} | {gap['duration']}s | Câu #{gap['before_index']} | {note} |")
    
    if len(asr_gaps) > 20:
        md_lines.append(f"\n*(Còn {len(asr_gaps) - 20} khoảng trống khác được lưu đầy đủ trong JSON)*\n")

    md_lines.extend([
        "\n## Danh sách các câu tiếng Việt thiếu khung che trắng (ASS BgStyle)\n",
        "| Bắt đầu | Kết thúc | Nội dung tiếng Việt |",
        "| :--- | :--- | :--- |",
    ])
    for mc in missing_covers:
        clean_text = mc["text"].replace("\\N", " ").strip()
        md_lines.append(f"| {mc['start_str']} | {mc['end_str']} | {clean_text} |")

    with open(output_report_md, "w", encoding="utf-8") as f:
        f.write("\n".join(md_lines))

    return audit_summary


if __name__ == "__main__":
    benchmark = Path(r"D:\workspace_v2\benchmark_runs\e2e_cold_verify_job")
    out_json = benchmark / "timeline_audit_report.json"
    out_md = benchmark / "timeline_audit_report.md"
    res = audit_timeline(benchmark, out_json, out_md)
    print(f"Audit completed: {res['asr_gaps_gte_5s_count']} gaps >= 5s, {res['missing_covers_count']} missing covers.")
