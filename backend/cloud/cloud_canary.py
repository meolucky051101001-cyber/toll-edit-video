"""
cloud_canary.py - Cổng kiểm tra trước khi chạy Canary GPU thật & Bộ đối chiếu thành phẩm Local vs Cloud.

Giai đoạn 7:
- Chặn chạy Canary thật nếu thiếu xác nhận người dùng, vượt trần chi phí CU, hoặc còn session chưa thu hồi.
- Kiểm thử tiền kiểm cục bộ (0 CU) cho đủ 3 kịch bản canary tối thiểu:
  (1) video ngắn, (2) video tên đặc biệt, (3) ca hủy.
- Đối chiếu cùng input giữa bản Local và Cloud theo 6 tiêu chí:
  đủ lời, vai giọng, phụ đề, timing, chất lượng video và chi phí/thu hồi tài nguyên.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import math
import re
import unicodedata
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

from .cloud_contract import (
    CloudCleanupStatus,
    CloudLedgerStatus,
    build_input_fingerprint,
    probe_video_metadata,
)
from .cloud_security import CloudValidationError, redact_secrets, validate_video_input


_CJK_CHAR_RE = re.compile(r"[\u4e00-\u9fff\u3400-\u4dbf]")
_SPECIAL_NAME_CHAR_RE = re.compile(r"[^A-Za-z0-9._-]")
_TERMINAL_CLEAN_LEDGER_STATUSES = {
    CloudLedgerStatus.RECONCILED.value,
    CloudLedgerStatus.RELEASED.value,
}


class CanaryPreconditionError(ValueError):
    """Raised when gated conditions for a bounded GPU Canary run are not met."""


def _redact_nested(value: Any) -> Any:
    if isinstance(value, str):
        return redact_secrets(value)
    if isinstance(value, dict):
        return {k: _redact_nested(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_redact_nested(item) for item in value]
    if isinstance(value, tuple):
        return [_redact_nested(item) for item in value]
    return value


def _probe_canary_local_file(
    raw_path: str | Path,
    *,
    label: str,
    allowed_roots: Optional[Sequence[str | Path]],
    require_special_name: bool = False,
) -> Dict[str, Any]:
    try:
        validated = validate_video_input(
            str(raw_path),
            allowed_roots=allowed_roots,
            require_exists=True,
        )
    except CloudValidationError as exc:
        raise CanaryPreconditionError(f"Canary [{label}] đầu vào không hợp lệ: {exc}") from exc

    if validated.kind != "local_file":
        raise CanaryPreconditionError(
            f"Canary [{label}] yêu cầu tệp video cục bộ xác định trước, không dùng URL: {validated.display_name}"
        )

    if require_special_name and not _SPECIAL_NAME_CHAR_RE.search(validated.display_name):
        raise CanaryPreconditionError(
            f"Canary [{label}] tên tệp '{validated.display_name}' chưa chứa khoảng trắng/Unicode/ký tự đặc biệt."
        )

    probe = probe_video_metadata(validated.normalized_value)
    if not probe.get("exists") or int(probe.get("size_bytes") or 0) <= 0 or not probe.get("header_valid"):
        raise CanaryPreconditionError(
            f"Canary [{label}] tệp '{validated.display_name}' hỏng container hoặc rỗng."
        )

    fp = build_input_fingerprint(validated.normalized_value, kind="local_file")
    return {
        "scenario": label,
        "path": validated.normalized_value,
        "display_name": validated.display_name,
        "fingerprint": fp,
        "probe": probe,
    }


def verify_canary_readiness(
    store: Any,
    *,
    user_approved: bool,
    max_cu_cap: float,
    short_video_path: str | Path,
    special_name_video_path: str | Path,
    cancel_case_video_path: str | Path,
    provider_usage: Optional[Dict[str, Any]] = None,
    allowed_roots: Optional[Sequence[str | Path]] = None,
) -> Dict[str, Any]:
    """
    Kiểm tra đầy đủ điều kiện tiên quyết (0 CU) trước khi cho phép chạy Canary GPU thật.
    Chặn ngay nếu chưa có xác nhận người dùng, thiếu ngân sách/số dư, còn session chưa thu hồi,
    hoặc bất kỳ file mẫu nào trong 3 kịch bản không hợp lệ.
    """
    if user_approved is not True:
        raise CanaryPreconditionError(
            "Chưa có xác nhận của người dùng (user_approved=True) cho danh sách video và trần chi phí Canary."
        )

    try:
        cu_cap = float(max_cu_cap)
    except (TypeError, ValueError) as exc:
        raise CanaryPreconditionError("Trần chi phí Canary (max_cu_cap) không hợp lệ.") from exc
    if cu_cap <= 0.0:
        raise CanaryPreconditionError("Trần chi phí Canary (max_cu_cap) phải lớn hơn 0 CU.")

    pending_cleanup = store.list_pending_cleanup_events()
    if pending_cleanup:
        raise CanaryPreconditionError(
            f"Đang có {len(pending_cleanup)} session Cloud chưa xác nhận thu hồi; phải dọn sạch trước khi Canary."
        )

    budget_info = store.get_cu_budget_info()
    if int(budget_info.get("unreconciled_count") or 0) > 0:
        raise CanaryPreconditionError(
            "Đang có khoản chi trong sổ chi phí chưa đối soát; không thể mở Canary mới."
        )

    remaining_today = float(budget_info.get("remaining_today") or 0.0)
    if remaining_today < cu_cap - 1e-6:
        raise CanaryPreconditionError(
            f"Ngân sách ngày còn lại ({remaining_today:.2f} CU) nhỏ hơn trần Canary ({cu_cap:.2f} CU)."
        )

    if not isinstance(provider_usage, dict) or not provider_usage.get("ok") or provider_usage.get("stale"):
        err_detail = provider_usage.get("error") if isinstance(provider_usage, dict) else "missing usage"
        raise CanaryPreconditionError(
            f"Không thể xác nhận số dư Colab tươi trước khi Canary ({redact_secrets(str(err_detail))})."
        )

    balance = provider_usage.get("balance")
    if balance is None or float(balance) < cu_cap - 1e-6:
        raise CanaryPreconditionError(
            f"Số dư Colab ({balance} CU) không đủ cho trần Canary ({cu_cap:.2f} CU)."
        )

    scenarios = {
        "short_video": _probe_canary_local_file(
            short_video_path,
            label="short_video",
            allowed_roots=allowed_roots,
            require_special_name=False,
        ),
        "special_name_video": _probe_canary_local_file(
            special_name_video_path,
            label="special_name_video",
            allowed_roots=allowed_roots,
            require_special_name=True,
        ),
        "cancel_case": _probe_canary_local_file(
            cancel_case_video_path,
            label="cancel_case",
            allowed_roots=allowed_roots,
            require_special_name=False,
        ),
    }

    manifest = {
        "ready": True,
        "checked_at_utc": dt.datetime.now(dt.timezone.utc).isoformat(),
        "user_approved": True,
        "max_cu_cap": round(cu_cap, 4),
        "provider_balance_cu": round(float(balance), 4),
        "budget": budget_info,
        "scenarios": scenarios,
    }
    return _redact_nested(manifest)


def _extract_cue_id(cue: Dict[str, Any], index: int) -> str:
    raw = cue.get("id") if cue.get("id") is not None else cue.get("cue_id")
    if raw is not None and str(raw).strip():
        return str(raw).strip()
    return str(index + 1)


def _extract_cue_text(cue: Dict[str, Any]) -> str:
    for key in ("vi", "text_vi", "translated", "text", "caption"):
        val = cue.get(key)
        if isinstance(val, str) and val.strip():
            return val.strip()
    return ""


def _extract_cue_role(cue: Dict[str, Any]) -> str:
    for key in ("role", "speaker", "speaker_role"):
        val = cue.get(key)
        if isinstance(val, str) and val.strip():
            norm = val.strip().lower()
            if norm in ("nu", "nữ", "female", "f"):
                return "female"
            if norm in ("nam", "male", "m"):
                return "male"
            return norm
    voice = cue.get("voice")
    if isinstance(voice, str) and voice.strip():
        return voice.strip().lower()
    return ""


def _normalize_source_text(value: str) -> str:
    """Ignore typography, but retain numeric punctuation and symbols that change meaning."""
    text = unicodedata.normalize("NFKC", value).casefold()
    chars = []
    for i, char in enumerate(text):
        if char.isspace():
            continue
        if unicodedata.category(char).startswith("P"):
            # Do not equate 1.5 with 15, -5 with 5, or 1/2 with 12.
            numeric = char in ".,+-/:" and (
                (i > 0 and text[i - 1].isdigit())
                or (i + 1 < len(text) and text[i + 1].isdigit())
            )
            if not numeric and char not in "%‰":
                continue
        chars.append(char)
    return "".join(chars)


def compare_source_cues(
    *,
    reference_cues: Sequence[Dict[str, Any]],
    candidate_cues: Sequence[Dict[str, Any]],
    reference_provenance: Dict[str, Any],
    candidate_source_sha256: str,
    max_boundary_drift_sec: float = 0.5,
    max_reference_group: int = 3,
    max_candidate_group: int = 4,
) -> Dict[str, Any]:
    """Compare source transcripts across different segmentations, explicitly opt-in.

    Source fields are ``source_text`` or ``source``; translated ``text``/``vi``
    fields are never inferred to be source. A bounded monotone alignment uses
    text only. Timing is measured afterwards, never used to select a match.
    Candidate similarity finds review pairs, not content acceptance: every
    group must be exact after conservative normalization for content to pass.

    Provenance is recorded, not independently authenticated. A reviewed reference
    requires reviewer/evidence details and both inputs must identify the same
    source hash. Passing this function does not certify translation, rendered ASS,
    TTS word timing, voice roles, or full Canary acceptance.
    """
    if not math.isfinite(max_boundary_drift_sec) or max_boundary_drift_sec < 0:
        raise ValueError("max_boundary_drift_sec must be finite and nonnegative")
    if not (1 <= max_reference_group <= 4 and 1 <= max_candidate_group <= 4):
        raise ValueError("Alignment groups must contain 1 to 4 consecutive cues")
    if len(reference_cues) * len(candidate_cues) > 40000:
        raise ValueError("Too many cues for bounded source alignment")

    errors: List[str] = []
    provenance = dict(reference_provenance or {})
    ref_hash = str(provenance.get("source_sha256") or "").lower()
    candidate_hash = str(candidate_source_sha256 or "").lower()
    if not re.fullmatch(r"[a-f0-9]{64}", ref_hash) or ref_hash != candidate_hash:
        errors.append("Reference and candidate must identify the same source SHA256")
    if provenance.get("kind") not in ("local_baseline", "reviewed_reference"):
        errors.append("Reference kind must be local_baseline or reviewed_reference")
    if not str(provenance.get("artifact") or "").strip():
        errors.append("Reference artifact is required")
    if provenance.get("kind") == "reviewed_reference" and not (
        str(provenance.get("reviewed_by") or "").strip()
        and provenance.get("evidence")
    ):
        errors.append("Reviewed reference requires reviewer and evidence")

    def prepare(cues, side):
        rows = []
        ids = set()
        previous_end = -1.0
        for index, cue in enumerate(cues):
            cid = _extract_cue_id(cue, index)
            raw = cue.get("source_text", cue.get("source"))
            source = raw if isinstance(raw, str) else ""
            normalized = _normalize_source_text(source)
            if cid in ids:
                errors.append(f"{side}: duplicate cue ID {cid}")
            ids.add(cid)
            if not normalized:
                errors.append(f"{side}: cue {cid} requires nonempty source_text/source")
            if len(normalized) > 1000:
                raise ValueError("A source cue exceeds the 1000-character alignment limit")
            try:
                start, end = float(cue["start"]), float(cue["end"])
                valid = math.isfinite(start) and math.isfinite(end) and 0 <= start < end
                if valid and start < previous_end - 1e-6:
                    errors.append(f"{side}: cue {cid} is out of order or overlaps")
                if valid:
                    previous_end = end
            except (KeyError, TypeError, ValueError):
                start, end, valid = None, None, False
            if not valid:
                errors.append(f"{side}: cue {cid} has an invalid interval")
                start, end = None, None
            rows.append({"id": cid, "source": source, "normalized": normalized,
                         "start": start, "end": end})
        return rows

    reference = prepare(reference_cues, "reference")
    candidate = prepare(candidate_cues, "candidate")
    if not reference or not candidate:
        errors.append("Both source transcripts must contain cues")
    n, m = len(reference), len(candidate)

    @lru_cache(maxsize=None)
    def group_text(side, start, count):
        rows = reference if side == "reference" else candidate
        return "".join(row["normalized"] for row in rows[start:start + count])

    @lru_cache(maxsize=32768)
    def edit_distance(left, right):
        prev = list(range(len(right) + 1))
        for i, char in enumerate(left, 1):
            row = [i]
            for j, other in enumerate(right, 1):
                row.append(min(row[-1] + 1, prev[j] + 1, prev[j - 1] + (char != other)))
            prev = row
        return prev[-1]

    # Integer quarter-unit costs avoid floating-point tie ambiguity. Store one
    # path for display and cap the number of equally optimal paths at two.
    scores = {(0, 0): 0}
    ways = {(0, 0): 1}
    predecessor = {}
    for i in range(n + 1):
        for j in range(m + 1):
            if (i, j) not in scores:
                continue
            choices = []
            if i < n:
                choices.append((1, 0, len(reference[i]["normalized"]) * 4 + 2))
            if j < m:
                choices.append((0, 1, len(candidate[j]["normalized"]) * 4 + 2))
            for a in range(1, min(max_reference_group, n - i) + 1):
                for b in range(1, min(max_candidate_group, m - j) + 1):
                    left, right = group_text("reference", i, a), group_text("candidate", j, b)
                    if not left or not right:
                        continue
                    edits = edit_distance(left, right)
                    if 1.0 - edits / max(len(left), len(right)) < 0.45:
                        continue
                    choices.append((a, b, 4 * edits + 3 * (a + b - 2)))
            for a, b, cost in choices:
                target = (i + a, j + b)
                score = scores[i, j] + cost
                if target not in scores or score < scores[target]:
                    scores[target] = score
                    ways[target] = ways[i, j]
                    predecessor[target] = (i, j, a, b)
                elif score == scores[target]:
                    ways[target] = min(2, ways[target] + ways[i, j])

    path = []
    cursor = (n, m)
    while cursor != (0, 0):
        edge = predecessor[cursor]
        path.append(edge)
        cursor = edge[0], edge[1]
    path.reverse()
    groups = []
    for i, j, a, b in path:
        left_rows, right_rows = reference[i:i + a], candidate[j:j + b]
        left, right = group_text("reference", i, a), group_text("candidate", j, b)
        edits = edit_distance(left, right)
        row = {
            "reference_ids": [r["id"] for r in left_rows],
            "candidate_ids": [r["id"] for r in right_rows],
            "reference_text": " / ".join(r["source"] for r in left_rows),
            "candidate_text": " / ".join(r["source"] for r in right_rows),
            "exact_source_match": bool(a and b and left and left == right),
            "character_edit_distance": edits,
            "candidate_similarity": round(1.0 - edits / max(len(left), len(right), 1), 4),
            "max_boundary_drift_sec": None,
        }
        if a and b and all(r["start"] is not None for r in left_rows + right_rows):
            row["reference_interval"] = [left_rows[0]["start"], left_rows[-1]["end"]]
            row["candidate_interval"] = [right_rows[0]["start"], right_rows[-1]["end"]]
            row["start_delta_sec"] = round(right_rows[0]["start"] - left_rows[0]["start"], 6)
            row["end_delta_sec"] = round(right_rows[-1]["end"] - left_rows[-1]["end"], 6)
            row["max_boundary_drift_sec"] = max(abs(row["start_delta_sec"]), abs(row["end_delta_sec"]))
        groups.append(row)

    ambiguous = ways[n, m] > 1
    unmatched_reference = [cid for row in groups if not row["candidate_ids"] for cid in row["reference_ids"]]
    unmatched_candidate = [cid for row in groups if not row["reference_ids"] for cid in row["candidate_ids"]]
    source_matches = bool(groups) and all(row["exact_source_match"] for row in groups)
    content_passed = source_matches and not errors and not ambiguous
    drifts = [row["max_boundary_drift_sec"] for row in groups if row["max_boundary_drift_sec"] is not None]
    boundary_passed = bool(groups) and len(drifts) == len(groups) and not errors and not ambiguous
    boundary_passed = boundary_passed and max(drifts, default=math.inf) <= max_boundary_drift_sec
    return _redact_nested({
        "passed": content_passed and boundary_passed,
        "scope": "source_content_and_group_boundaries_only",
        "method": "bounded_monotone_source_text_alignment_v1",
        "time_used_for_matching": False,
        "reference_provenance": provenance,
        "candidate_source_sha256": candidate_hash,
        "reference_provenance_independently_verified": False,
        "reference_count": n, "candidate_count": m,
        "groups": groups,
        "ambiguous": ambiguous,
        "optimal_path_count_capped_at_two": ways[n, m],
        "unmatched_reference_ids": unmatched_reference,
        "unmatched_candidate_ids": unmatched_candidate,
        "content_passed": content_passed,
        "group_boundary_timing_passed": boundary_passed,
        "max_boundary_drift_sec": max(drifts) if drifts else None,
        "max_allowed_boundary_drift_sec": max_boundary_drift_sec,
        "needs_review": bool(errors or ambiguous or not content_passed or not boundary_passed),
        "validation_errors": errors,
        "limitations": [
            "Exact source agreement does not validate the Vietnamese translation.",
            "Group boundaries cannot certify internal word, rendered ASS, or TTS timing.",
            "Reference provenance must be audited by the caller; matching a baseline does not make it ground truth.",
            "This report alone cannot open the Phase 8 gate or override the six-criterion Canary report.",
        ],
    })


def compare_local_vs_cloud_deliverables(
    *,
    local_video_path: str | Path,
    local_cues: Sequence[Dict[str, Any]],
    cloud_manifest: Dict[str, Any],
    cloud_cues: Sequence[Dict[str, Any]],
    local_probe: Optional[Dict[str, Any]] = None,
    ledger_entry: Optional[Dict[str, Any]] = None,
    cleanup_status: str = CloudCleanupStatus.VERIFIED.value,
    max_cu_cap: float = 5.0,
    max_duration_diff_sec: float = 1.5,
    max_timing_drift_sec: float = 0.5,
    source_alignment_request: Optional[Dict[str, Any]] = None,
    matching_mode: str = "cue_id",
    acceptance_evidence: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """
    Đối chiếu thành phẩm Local vs Cloud trên cùng video nguồn theo 6 nhóm tiêu chí Giai đoạn 7.
    Không suy diễn kết quả của một video thành tỷ lệ chính xác tổng quát.
    """
    if matching_mode not in ("cue_id", "source_alignment"):
        raise ValueError("matching_mode must be cue_id or source_alignment")
    if matching_mode == "source_alignment" and source_alignment_request is None:
        raise ValueError("source_alignment mode requires an explicit source_alignment_request")
    failures: List[str] = []
    warnings: List[str] = []

    l_by_id: Dict[str, Dict[str, Any]] = {}
    l_order: List[str] = []
    for idx, cue in enumerate(local_cues or ()):
        cid = _extract_cue_id(cue, idx)
        l_by_id[cid] = cue
        l_order.append(cid)

    c_by_id: Dict[str, Dict[str, Any]] = {}
    c_order: List[str] = []
    for idx, cue in enumerate(cloud_cues or ()):
        cid = _extract_cue_id(cue, idx)
        c_by_id[cid] = cue
        c_order.append(cid)

    # 1. Đủ lời (Cue completeness)
    missing_ids = [cid for cid in l_order if cid not in c_by_id]
    extra_ids = [cid for cid in c_order if cid not in l_by_id]
    empty_vi_ids = [cid for cid, cue in c_by_id.items() if not _extract_cue_text(cue)]
    cjk_leaked_ids = [cid for cid, cue in c_by_id.items() if _CJK_CHAR_RE.search(_extract_cue_text(cue))]
    cue_pass = (
        len(l_order) > 0
        and len(c_order) == len(l_order)
        and not missing_ids
        and not extra_ids
        and not empty_vi_ids
        and not cjk_leaked_ids
    )
    if not cue_pass:
        failures.append(
            f"Lệch đủ lời giữa Local ({len(l_order)}) và Cloud ({len(c_order)}): "
            f"thiếu={missing_ids}, thừa={extra_ids}, rỗng={empty_vi_ids}, sót_CJK={cjk_leaked_ids}."
        )

    # 2. Vai giọng (Voice/Role parity)
    role_mismatches: List[Dict[str, str]] = []
    compared_roles = 0
    for cid in l_order:
        if cid not in c_by_id:
            continue
        l_role = _extract_cue_role(l_by_id[cid])
        c_role = _extract_cue_role(c_by_id[cid])
        if l_role and c_role:
            compared_roles += 1
            if l_role != c_role:
                role_mismatches.append({"cue_id": cid, "local": l_role, "cloud": c_role})
    role_pass = len(role_mismatches) == 0
    if not role_pass:
        failures.append(
            f"Lệch vai giọng ở {len(role_mismatches)}/{compared_roles} câu (cần kiểm tra lại giọng nam/nữ)."
        )

    # 3. Phụ đề (Subtitle integrity)
    invalid_intervals: List[str] = []
    for cid in c_order:
        cue = c_by_id[cid]
        try:
            st = float(cue.get("start", 0.0))
            en = float(cue.get("end", 0.0))
        except (TypeError, ValueError):
            invalid_intervals.append(cid)
            continue
        if st < 0.0 or en <= st:
            invalid_intervals.append(cid)
    sub_pass = len(c_order) > 0 and not empty_vi_ids and not invalid_intervals
    if not sub_pass:
        failures.append(
            f"Phụ đề Cloud không hợp lệ: số câu={len(c_order)}, rỗng={empty_vi_ids}, sai timing={invalid_intervals}."
        )

    # 4. Timing parity
    drifts: List[float] = []
    for cid in l_order:
        if cid not in c_by_id:
            continue
        try:
            l_st = float(l_by_id[cid].get("start", 0.0))
            l_en = float(l_by_id[cid].get("end", 0.0))
            c_st = float(c_by_id[cid].get("start", 0.0))
            c_en = float(c_by_id[cid].get("end", 0.0))
            drifts.append(max(abs(c_st - l_st), abs(c_en - l_en)))
        except (TypeError, ValueError):
            drifts.append(float("inf"))

    max_drift = max(drifts) if drifts else 0.0
    mean_drift = (sum(drifts) / len(drifts)) if drifts else 0.0

    resolved_local_probe = (
        dict(local_probe)
        if isinstance(local_probe, dict)
        else probe_video_metadata(local_video_path)
    )
    local_dur = resolved_local_probe.get("duration_seconds")
    cloud_dur = cloud_manifest.get("duration_seconds")
    dur_diff: Optional[float] = None
    dur_ok = True
    if local_dur is not None and cloud_dur is not None:
        try:
            dur_diff = abs(float(cloud_dur) - float(local_dur))
            dur_ok = dur_diff <= float(max_duration_diff_sec)
        except (TypeError, ValueError):
            dur_ok = False
    elif local_dur is None and cloud_dur is None:
        warnings.append("Chưa có số đo duration của local và cloud để đối chiếu thời lượng video.")

    timing_pass = (
        len(drifts) > 0
        and max_drift <= float(max_timing_drift_sec)
        and dur_ok
    )
    if not timing_pass:
        failures.append(
            f"Lệch timing vượt ngưỡng: max_cue_drift={max_drift:.3f}s (ngưỡng {max_timing_drift_sec}s), "
            f"video_duration_diff={dur_diff}."
        )

    # 5. Chất lượng video
    qc_passed_flag = bool(cloud_manifest.get("qc_passed", False))
    result_path_str = str(cloud_manifest.get("result_path") or "")
    size_bytes = int(cloud_manifest.get("size_bytes") or 0)
    if size_bytes <= 0 and result_path_str and Path(result_path_str).is_file():
        size_bytes = Path(result_path_str).stat().st_size
    v_codec = cloud_manifest.get("video_codec") or (
        (cloud_manifest.get("remote_metadata") or {}).get("output_video_probe") or {}
    ).get("video_codec")
    a_codec = cloud_manifest.get("audio_codec") or (
        (cloud_manifest.get("remote_metadata") or {}).get("output_video_probe") or {}
    ).get("audio_codec")

    local_w = resolved_local_probe.get("width")
    local_h = resolved_local_probe.get("height")
    cloud_probe_meta = (cloud_manifest.get("remote_metadata") or {}).get("output_video_probe") or {}
    cloud_w = cloud_manifest.get("width") if cloud_manifest.get("width") is not None else cloud_probe_meta.get("width")
    cloud_h = cloud_manifest.get("height") if cloud_manifest.get("height") is not None else cloud_probe_meta.get("height")
    resolution_match: Optional[bool] = None
    if local_w and local_h and cloud_w and cloud_h:
        resolution_match = (int(local_w) == int(cloud_w)) and (int(local_h) == int(cloud_h))

    video_pass = (
        qc_passed_flag
        and size_bytes > 0
        and bool(v_codec)
        and bool(a_codec)
        and (resolution_match is not False)
    )
    if not video_pass:
        failures.append(
            f"Chất lượng video không đạt: qc_passed={qc_passed_flag}, size_bytes={size_bytes}, "
            f"video_codec={v_codec}, audio_codec={a_codec}, resolution_match={resolution_match}."
        )

    # 6. Chi phí & Thu hồi tài nguyên
    raw_cleanup = str(cleanup_status or "")
    ledger_status = str(ledger_entry.get("status") or "") if isinstance(ledger_entry, dict) else ""
    cu_val = None
    if isinstance(ledger_entry, dict):
        cu_val = (
            ledger_entry.get("cu_reconciled")
            if ledger_entry.get("cu_reconciled") is not None
            else ledger_entry.get("cu_observed")
        )
    if cu_val is None:
        cu_val = cloud_manifest.get("cu_actual", 0.0)
    try:
        cu_used = float(cu_val or 0.0)
    except (TypeError, ValueError):
        cu_used = float("inf")

    ledger_ok = True if ledger_entry is None else (ledger_status in _TERMINAL_CLEAN_LEDGER_STATUSES)
    cost_pass = (
        raw_cleanup == CloudCleanupStatus.VERIFIED.value
        and ledger_ok
        and cu_used <= float(max_cu_cap) + 1e-6
    )
    if not cost_pass:
        failures.append(
            f"Chi phí/thu hồi chưa đạt: cleanup={raw_cleanup}, ledger={ledger_status or 'N/A'}, "
            f"cu={cu_used:.4f} (trần {float(max_cu_cap):.4f})."
        )

    disclaimer = (
        "Kết quả đối chiếu trên một video mẫu không phải tỷ lệ chính xác tổng quát cho mọi video "
        "và không tự động áp dụng quy tắc đặc thù của video mẫu cho các video khác."
    )
    if role_mismatches or not role_pass:
        warnings.append(disclaimer)

    dimensions = {
        "cue_completeness": {
            "passed": cue_pass,
            "local_count": len(l_order),
            "cloud_count": len(c_order),
            "missing_ids": missing_ids,
            "extra_ids": extra_ids,
            "empty_vi_ids": empty_vi_ids,
            "cjk_leaked_ids": cjk_leaked_ids,
        },
        "voice_role_parity": {
            "passed": role_pass,
            "compared_cues": compared_roles,
            "mismatches": role_mismatches,
            "needs_human_review": bool(role_mismatches),
            "generalizable_accuracy_claim": False,
        },
        "subtitle_integrity": {
            "passed": sub_pass,
            "total_cues": len(c_order),
            "invalid_intervals": invalid_intervals,
        },
        "timing_parity": {
            "passed": timing_pass,
            "max_abs_drift_sec": round(max_drift, 4),
            "mean_abs_drift_sec": round(mean_drift, 4),
            "video_duration_diff_sec": round(dur_diff, 4) if dur_diff is not None else None,
        },
        "video_quality": {
            "passed": video_pass,
            "qc_passed": qc_passed_flag,
            "size_bytes": size_bytes,
            "video_codec": v_codec,
            "audio_codec": a_codec,
            "resolution_match": resolution_match,
        },
        "cost_and_cleanup": {
            "passed": cost_pass,
            "cleanup_status": raw_cleanup,
            "ledger_status": ledger_status or None,
            "cu_used": round(cu_used, 4),
            "max_cu_cap": float(max_cu_cap),
        },
    }

    source_report = compare_source_cues(**source_alignment_request) if source_alignment_request is not None else None
    if matching_mode == "source_alignment":
        binding_errors = []
        source_candidates = source_alignment_request["candidate_cues"]
        source_references = source_alignment_request["reference_cues"]
        source_by_id = {_extract_cue_id(cue, idx): cue for idx, cue in enumerate(source_candidates)}
        refs_by_id = {_extract_cue_id(cue, idx): cue for idx, cue in enumerate(source_references)}
        if len(c_order) != len(c_by_id) or list(source_by_id) != c_order:
            binding_errors.append("Source alignment candidate IDs must cover exactly the delivered Cloud cues")
        for cid in set(source_by_id) & set(c_by_id):
            try:
                if any(not math.isfinite(float(c_by_id[cid][key]))
                       or abs(float(source_by_id[cid][key]) - float(c_by_id[cid][key])) > 0.001
                       for key in ("start", "end")):
                    binding_errors.append(f"Source alignment candidate timing differs from deliverable cue {cid}")
            except (KeyError, TypeError, ValueError):
                binding_errors.append(f"Cannot bind source alignment to deliverable cue {cid}")

        try:
            evidence_identity = build_canary_evidence_identity(
                source_sha256=source_alignment_request["candidate_source_sha256"],
                output_video_path=result_path_str, source_cues=source_candidates, cloud_cues=cloud_cues,
            )
            declared_output_hash = cloud_manifest.get("sha256")
            if declared_output_hash and declared_output_hash != evidence_identity["output_sha256"]:
                binding_errors.append("Cloud output checksum does not match its manifest")
        except (KeyError, TypeError, ValueError, OSError) as exc:
            evidence_identity = None
            binding_errors.append(f"Cannot bind acceptance evidence to output/cues: {exc}")
        evidence_results = {}
        for kind in ("translation_review", "rendered_timing", "tts_timing"):
            evidence_results[kind] = _validate_acceptance_evidence(
                kind, (acceptance_evidence or {}).get(kind), evidence_identity,
            )
        # Caller-authored evidence must be concrete and checksum bound. Its
        # findings remain auditable assertions, not a substitute for the checks.
        source_ok = not binding_errors and source_report["content_passed"]
        source_timing_ok = not binding_errors and source_report["group_boundary_timing_passed"]
        dimensions["cue_completeness"].update(
            passed=source_ok and not empty_vi_ids and not cjk_leaked_ids and evidence_results["translation_review"]["passed"],
            matching_mode=matching_mode,
            reference_count=source_report["reference_count"],
            missing_ids=source_report["unmatched_reference_ids"],
            extra_ids=source_report["unmatched_candidate_ids"],
            ambiguous=source_report["ambiguous"],
            content_passed=source_report["content_passed"],
            translation_review=evidence_results["translation_review"],
        )
        aligned_role_errors = []
        compared_groups = 0
        def roles_for(rows):
            roles = []
            for cue in rows:
                role = _extract_cue_role(cue)
                if not role:
                    return None
                if not roles or role != roles[-1]:
                    roles.append(role)
            return roles
        for group in source_report["groups"]:
            r_roles = roles_for([refs_by_id[cid] for cid in group["reference_ids"]])
            c_roles = roles_for([c_by_id[cid] for cid in group["candidate_ids"] if cid in c_by_id])
            if not r_roles or not c_roles or r_roles != c_roles:
                aligned_role_errors.append({"reference_ids": group["reference_ids"], "candidate_ids": group["candidate_ids"],
                                            "reference_roles": r_roles, "candidate_roles": c_roles})
            else:
                compared_groups += 1
        dimensions["voice_role_parity"].update(
            passed=bool(compared_groups) and not aligned_role_errors and not binding_errors and not source_report["ambiguous"],
            compared_cues=None, compared_groups=compared_groups, mismatches=aligned_role_errors,
            needs_human_review=bool(aligned_role_errors or source_report["ambiguous"]),
        )
        dimensions["subtitle_integrity"].update(
            passed=sub_pass and evidence_results["rendered_timing"]["passed"],
            rendered_timing=evidence_results["rendered_timing"],
        )
        dimensions["timing_parity"].update(
            passed=source_timing_ok and dur_ok and evidence_results["rendered_timing"]["passed"] and evidence_results["tts_timing"]["passed"],
            max_abs_drift_sec=source_report["max_boundary_drift_sec"], mean_abs_drift_sec=None,
            matching_mode=matching_mode, drift_scope="source_group_outer_boundaries",
            rendered_timing=evidence_results["rendered_timing"], tts_timing=evidence_results["tts_timing"],
        )
        dimensions["video_quality"]["passed"] = video_pass and evidence_identity is not None and not binding_errors
        failures = [f"Không đạt {name}; xem chi tiết dimension." for name, dim in dimensions.items() if not dim["passed"]]
        failures.extend(binding_errors)
        warnings.append("Reference được chọn rõ ràng; căn nhóm không chứng nhận nội bộ từng từ. ASS/TTS và bản dịch có bằng chứng riêng.")

    overall_passed = all(bool(dim["passed"]) for dim in dimensions.values())
    report = {
        "passed": overall_passed,
        "local_video_path": str(local_video_path),
        "cloud_result_path": result_path_str,
        "dimensions": dimensions,
        "failures": failures,
        "warnings": warnings,
        "disclaimer": disclaimer,
        "matching_mode": matching_mode,
    }
    if source_report is not None:
        report["source_alignment"] = source_report
        # Source evidence never silently replaces the default legacy comparison;
        # the alternative mode above must be requested explicitly.
        if not source_report["passed"]:
            report["passed"] = False
            report["failures"].append("Đối chiếu nội dung nguồn/ranh giới nhóm chưa đạt; xem source_alignment.")
        report["warnings"].append("Source alignment chỉ đo nội dung nguồn và ranh giới nhóm, không thay QC ASS/TTS.")
    return _redact_nested(report)


def build_canary_evidence_identity(
    *, source_sha256: str, output_video_path: str | Path,
    source_cues: Sequence[Dict[str, Any]], cloud_cues: Sequence[Dict[str, Any]],
) -> Dict[str, str]:
    """Identity to embed inside each evidence JSON, not merely its outer pointer.

    Both the rendered output bytes and exact source/VI/role/timing cue snapshot
    are bound. Reusing a passing audit from another output or translation fails.
    The caller separately verifies that source_sha256 belongs to the input file.
    """
    source_digest = str(source_sha256).lower()
    if not re.fullmatch(r"[a-f0-9]{64}", source_digest):
        raise ValueError("source_sha256 must identify the verified input")
    if not output_video_path or not Path(output_video_path).is_file():
        raise ValueError("A saved output video is required for acceptance evidence")
    if len(source_cues) != len(cloud_cues) or not cloud_cues:
        raise ValueError("Evidence needs equal nonempty source and translated cue snapshots")
    rows = []
    for i, (source, translated) in enumerate(zip(source_cues, cloud_cues)):
        sid, cid = _extract_cue_id(source, i), _extract_cue_id(translated, i)
        if sid != cid:
            raise ValueError("Source/translated cue identity or order differs")
        source_text = source.get("source_text", source.get("source"))
        if not isinstance(source_text, str) or not source_text.strip():
            raise ValueError("Evidence needs explicit source text")
        timings = {key: float(translated[key]) for key in ("start", "end")}
        if not all(math.isfinite(value) for value in timings.values()):
            raise ValueError("Evidence cannot bind nonfinite cue timestamps")
        if not 0 <= timings["start"] < timings["end"]:
            raise ValueError("Evidence cannot bind an invalid cue interval")
        if any(abs(float(source[key]) - timings[key]) > 0.001 for key in timings):
            raise ValueError("Source and translated cue intervals differ")
        rows.append({"id": cid, **timings, "source": source_text,
                     "vi": _extract_cue_text(translated), "role": _extract_cue_role(translated)})
    cue_bytes = json.dumps(rows, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")
    digest = hashlib.sha256()
    with Path(output_video_path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return {"source_sha256": source_digest, "output_sha256": digest.hexdigest(),
            "candidate_cues_sha256": hashlib.sha256(cue_bytes).hexdigest()}


def _validate_acceptance_evidence(kind, item, identity):
    item = item if isinstance(item, dict) else {}
    artifact = str(item.get("artifact") or "")
    digest = str(item.get("artifact_sha256") or "").lower()
    errors = []
    payload = None
    try:
        raw = Path(artifact).read_bytes()
        if hashlib.sha256(raw).hexdigest() != digest:
            errors.append("Evidence artifact checksum mismatch")
        payload = json.loads(raw.decode("utf-8-sig"))
    except (OSError, ValueError, UnicodeError):
        errors.append("Evidence artifact must be an existing JSON report")
    if not identity:
        errors.append("Output/candidate identity is not verified")
    if not isinstance(payload, dict):
        errors.append("Evidence report must be a JSON object")
    else:
        if payload.get("schema") != 1 or payload.get("kind") != kind:
            errors.append("Evidence schema/kind mismatch")
        if payload.get("passed") is not True:
            errors.append("Evidence report is not passing")
        if not str(payload.get("method") or "").strip():
            errors.append("Evidence method/review description is required")
        checks = payload.get("checks")
        if not isinstance(checks, list) or not checks or any(
            not isinstance(check, dict) or check.get("passed") is not True
            or not str(check.get("name") or "").strip() for check in checks
        ):
            errors.append("Evidence requires named passing checks")
        for key, expected in (identity or {}).items():
            if payload.get(key) != expected:
                errors.append(f"Evidence {key} does not match the candidate")
    return {"passed": not errors, "artifact": artifact, "artifact_sha256": digest,
            "validation_errors": errors, "binding": identity}


def render_canary_report_markdown(report: Dict[str, Any]) -> str:
    """Xuất biên bản đối chiếu Local vs Cloud dưới dạng Markdown đã khử bí mật."""
    safe = _redact_nested(report)
    verdict = "PASS" if safe.get("passed") else "FAIL"
    dims = safe.get("dimensions") or {}
    lines = [
        f"# Biên bản đối chiếu Canary Local vs Cloud — {verdict}",
        "",
        f"- Local input: `{safe.get('local_video_path', '')}`",
        f"- Cloud output: `{safe.get('cloud_result_path', '')}`",
        f"- Matching mode: `{safe.get('matching_mode', 'cue_id')}`",
        "",
        "| Chiều kiểm tra | Kết quả | Chi tiết chính |",
        "|---|---|---|",
        f"| Đủ lời (`cue_completeness`) | {'PASS' if dims.get('cue_completeness', {}).get('passed') else 'FAIL'} | "
        f"Local={dims.get('cue_completeness', {}).get('local_count')}, Cloud={dims.get('cue_completeness', {}).get('cloud_count')} |",
        f"| Vai giọng (`voice_role_parity`) | {'PASS' if dims.get('voice_role_parity', {}).get('passed') else 'FAIL'} | "
        f"Mismatches={len(dims.get('voice_role_parity', {}).get('mismatches') or [])} |",
        f"| Phụ đề (`subtitle_integrity`) | {'PASS' if dims.get('subtitle_integrity', {}).get('passed') else 'FAIL'} | "
        f"Cues={dims.get('subtitle_integrity', {}).get('total_cues')} |",
        f"| Timing (`timing_parity`) | {'PASS' if dims.get('timing_parity', {}).get('passed') else 'FAIL'} | "
        f"MaxDrift={dims.get('timing_parity', {}).get('max_abs_drift_sec')}s |",
        f"| Chất lượng video (`video_quality`) | {'PASS' if dims.get('video_quality', {}).get('passed') else 'FAIL'} | "
        f"QC={dims.get('video_quality', {}).get('qc_passed')}, Codecs={dims.get('video_quality', {}).get('video_codec')}/{dims.get('video_quality', {}).get('audio_codec')} |",
        f"| Chi phí & Thu hồi (`cost_and_cleanup`) | {'PASS' if dims.get('cost_and_cleanup', {}).get('passed') else 'FAIL'} | "
        f"CU={dims.get('cost_and_cleanup', {}).get('cu_used')} / {dims.get('cost_and_cleanup', {}).get('max_cu_cap')}, Cleanup={dims.get('cost_and_cleanup', {}).get('cleanup_status')} |",
        "",
        f"> Lưu ý: {safe.get('disclaimer', '')}",
    ]
    if safe.get("source_alignment"):
        aligned = safe["source_alignment"]
        provenance = aligned.get("reference_provenance") or {}
        lines.extend([
            "", "## Đối chiếu nguồn theo nhóm",
            f"- Reference: `{provenance.get('artifact', '')}` ({provenance.get('kind', '')}).",
            f"- Content={'PASS' if aligned.get('content_passed') else 'FAIL'}, "
            f"group boundary timing={'PASS' if aligned.get('group_boundary_timing_passed') else 'FAIL'}, "
            f"ambiguous={aligned.get('ambiguous')}.",
            "- Ranh giới nhóm không chứng nhận timing từng từ, ASS render hoặc TTS; các bằng chứng này được kiểm tra riêng.",
        ])
    if safe.get("failures"):
        lines.extend(["", "## Lỗi phát hiện"])
        for item in safe["failures"]:
            lines.append(f"- {item}")
    return redact_secrets("\n".join(lines))
