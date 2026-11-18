"""Content alignment must explain segmentation without laundering acceptance failures."""
import hashlib
import json

import pytest

from backend.cloud.cloud_canary import build_canary_evidence_identity, compare_local_vs_cloud_deliverables, compare_source_cues


SOURCE_HASH = "a" * 64


def cue(cid, start, end, source, role="female"):
    return {"id": cid, "start": start, "end": end, "source": source, "role": role}


def request(reference, candidate, **kwargs):
    return dict(reference_cues=reference, candidate_cues=candidate,
                reference_provenance={"kind": "local_baseline", "artifact": "local/source.json",
                                      "source_sha256": SOURCE_HASH},
                candidate_source_sha256=SOURCE_HASH, **kwargs)


def test_split_merge_matches_content_without_using_timestamps():
    reference = [cue(10, 0, 4, "先裁剪纸张，再装入盒子。")]
    candidate = [cue(1, 0.1, 1.9, "先裁剪纸张"), cue(2, 1.9, 4.1, "再装入盒子")]
    report = compare_source_cues(**request(reference, candidate))
    assert report["passed"] and not report["time_used_for_matching"]
    assert report["groups"][0]["reference_ids"] == ["10"]
    assert report["groups"][0]["candidate_ids"] == ["1", "2"]
    assert report["max_boundary_drift_sec"] == 0.1
    shifted = [dict(row, start=row["start"] + 5, end=row["end"] + 5) for row in candidate]
    moved = compare_source_cues(**request(reference, shifted))
    assert moved["content_passed"] and not moved["group_boundary_timing_passed"]
    assert moved["groups"][0]["candidate_ids"] == ["1", "2"]
    assert moved["max_boundary_drift_sec"] == 5.1


def test_similar_content_and_matching_time_do_not_pass_semantic_change():
    report = compare_source_cues(**request(
        [cue(1, 0, 2, "裁剪也特别舒适")], [cue(1, 0, 2, "材质也特别舒适")]))
    assert report["group_boundary_timing_passed"]
    assert not report["content_passed"] and not report["passed"]
    assert report["groups"][0]["character_edit_distance"] == 2


def test_missing_and_added_cues_stay_unmatched():
    reference = [cue(1, 0, 1, "开头"), cue(2, 2, 3, "完全遗漏"), cue(3, 4, 5, "结尾")]
    candidate = [cue(1, 0, 1, "开头"), cue(2, 4, 5, "结尾")]
    missing = compare_source_cues(**request(reference, candidate))
    assert missing["unmatched_reference_ids"] == ["2"]
    assert not missing["passed"]
    extra = compare_source_cues(**request(candidate, reference))
    assert extra["unmatched_candidate_ids"] == ["2"]
    assert not extra["passed"]


def test_repeated_text_ambiguity_cannot_be_resolved_by_better_timing():
    reference = [cue(1, 0, 1, "重复"), cue(2, 2, 3, "重复")]
    candidate = [cue(1, 2, 3, "重复")]
    report = compare_source_cues(**request(reference, candidate))
    assert report["ambiguous"] and report["optimal_path_count_capped_at_two"] == 2
    assert not report["passed"]


@pytest.mark.parametrize("reference,candidate", [("1.5元", "15元"), ("-5度", "5度"), ("折扣50%", "折扣50")])
def test_numeric_meaning_is_not_erased_by_normalization(reference, candidate):
    report = compare_source_cues(**request([cue(1, 0, 1, reference)], [cue(1, 0, 1, candidate)]))
    assert not report["content_passed"]


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), -1, "invalid"])
def test_invalid_source_intervals_cannot_pass(bad):
    report = compare_source_cues(**request([cue(1, 0, 1, "内容")], [cue(1, bad, 1, "内容")]))
    assert report["validation_errors"] and not report["passed"]


def test_source_fields_provenance_and_source_identity_are_required():
    reference = [cue(1, 0, 1, "内容")]
    candidate = [{"id": 1, "start": 0, "end": 1, "vi": "Nội dung", "text": "内容"}]
    report = compare_source_cues(**request(reference, candidate))
    assert not report["passed"] and any("source_text/source" in e for e in report["validation_errors"])
    args = request(reference, reference)
    args["reference_provenance"]["kind"] = "reviewed_reference"
    assert not compare_source_cues(**args)["passed"]
    args["reference_provenance"].update(reviewed_by="reviewer", evidence=["source frame and listening receipt"])
    assert compare_source_cues(**args)["passed"]
    args["candidate_source_sha256"] = "b" * 64
    assert not compare_source_cues(**args)["passed"]


def test_duplicate_ids_and_overlaps_are_rejected():
    reference = [cue(1, 0, 1, "甲"), cue(1, 0.5, 2, "乙")]
    report = compare_source_cues(**request(reference, reference))
    assert not report["passed"]
    assert any("duplicate" in e for e in report["validation_errors"])
    assert any("overlaps" in e for e in report["validation_errors"])


def comparison_args(tmp_path):
    reference = [cue(10, 0, 4, "先裁剪纸张再装入盒子")]
    candidate = [cue(1, 0, 2, "先裁剪纸张"), cue(2, 2, 4, "再装入盒子")]
    output = tmp_path / "output.mp4"
    output.write_bytes(b"rendered video test bytes")
    return dict(
        local_video_path="local.mp4", local_cues=[dict(reference[0], vi="Cắt giấy rồi bỏ hộp")],
        cloud_cues=[dict(candidate[0], vi="Cắt giấy"), dict(candidate[1], vi="Bỏ vào hộp")],
        cloud_manifest={"qc_passed": True, "size_bytes": output.stat().st_size, "duration_seconds": 5,
                        "result_path": str(output), "sha256": hashlib.sha256(output.read_bytes()).hexdigest(),
                        "video_codec": "h264", "audio_codec": "aac", "width": 720, "height": 1280},
        local_probe={"duration_seconds": 5, "width": 720, "height": 1280},
        ledger_entry={"status": "reconciled", "cu_reconciled": 0.2},
        cleanup_status="verified", source_alignment_request=request(reference, candidate),
    )


def evidence(tmp_path, args):
    identity = build_canary_evidence_identity(
        source_sha256=SOURCE_HASH, output_video_path=args["cloud_manifest"]["result_path"],
        source_cues=args["source_alignment_request"]["candidate_cues"], cloud_cues=args["cloud_cues"],
    )
    reports = {}
    for kind in ("translation_review", "rendered_timing", "tts_timing"):
        artifact = tmp_path / f"{kind}.json"
        artifact.write_text(json.dumps({"schema": 1, "kind": kind, "passed": True, **identity,
                                       "method": "test fixture", "checks": [{"name": "fixture", "passed": True}]}), encoding="utf-8")
        reports[kind] = {"artifact": str(artifact), "artifact_sha256": hashlib.sha256(artifact.read_bytes()).hexdigest()}
    return reports


def test_opt_in_alignment_requires_separate_render_tts_and_translation_evidence(tmp_path):
    args = comparison_args(tmp_path)
    legacy = compare_local_vs_cloud_deliverables(**args)
    assert legacy["matching_mode"] == "cue_id" and not legacy["passed"]
    assert legacy["source_alignment"]["passed"]
    unverified = compare_local_vs_cloud_deliverables(**args, matching_mode="source_alignment")
    assert not unverified["passed"]
    verified = compare_local_vs_cloud_deliverables(**args, matching_mode="source_alignment", acceptance_evidence=evidence(tmp_path, args))
    assert verified["passed"]
    assert verified["dimensions"]["voice_role_parity"]["compared_groups"] == 1
    assert verified["dimensions"]["timing_parity"]["drift_scope"] == "source_group_outer_boundaries"


def test_changed_evidence_and_unbound_cues_cannot_pass(tmp_path):
    args = comparison_args(tmp_path)
    checked = evidence(tmp_path, args)
    (tmp_path / "rendered_timing.json").write_text("changed", encoding="utf-8")
    assert not compare_local_vs_cloud_deliverables(**args, matching_mode="source_alignment", acceptance_evidence=checked)["passed"]
    checked = evidence(tmp_path, args)
    args["cloud_cues"][0]["start"] = 0.25
    report = compare_local_vs_cloud_deliverables(**args, matching_mode="source_alignment", acceptance_evidence=checked)
    assert not report["passed"]
    assert any("differs from deliverable" in error for error in report["failures"])


def test_aligned_roles_require_complete_matching_evidence(tmp_path):
    args = comparison_args(tmp_path)
    args["cloud_cues"][1]["role"] = "male"
    report = compare_local_vs_cloud_deliverables(**args, matching_mode="source_alignment", acceptance_evidence=evidence(tmp_path, args))
    assert not report["passed"]
    assert not report["dimensions"]["voice_role_parity"]["passed"]


def test_good_evidence_never_overrides_content_mismatch(tmp_path):
    args = comparison_args(tmp_path)
    args["source_alignment_request"]["candidate_cues"][0]["source"] = "先抚摸纸张"
    report = compare_local_vs_cloud_deliverables(**args, matching_mode="source_alignment", acceptance_evidence=evidence(tmp_path, args))
    assert not report["passed"]
    assert not report["dimensions"]["cue_completeness"]["passed"]


def test_evidence_from_another_output_or_translation_cannot_pass(tmp_path):
    args = comparison_args(tmp_path)
    checked = evidence(tmp_path, args)
    output = tmp_path / "different-output.mp4"
    output.write_bytes(b"different output bytes")
    args["cloud_manifest"].update(result_path=str(output), sha256=hashlib.sha256(output.read_bytes()).hexdigest())
    report = compare_local_vs_cloud_deliverables(**args, matching_mode="source_alignment", acceptance_evidence=checked)
    assert not report["passed"]
    assert "Evidence output_sha256 does not match the candidate" in report["dimensions"]["subtitle_integrity"]["rendered_timing"]["validation_errors"]
    args = comparison_args(tmp_path)
    args["cloud_cues"][0]["vi"] = "Một bản dịch khác"
    report = compare_local_vs_cloud_deliverables(**args, matching_mode="source_alignment", acceptance_evidence=checked)
    assert not report["passed"]
    assert "Evidence candidate_cues_sha256 does not match the candidate" in report["dimensions"]["cue_completeness"]["translation_review"]["validation_errors"]


@pytest.mark.parametrize("change", ["source", "kind", "failed_check", "no_checks", "outer_boolean_only"])
def test_report_contents_not_just_outer_hash_are_required(tmp_path, change):
    args = comparison_args(tmp_path)
    checked = evidence(tmp_path, args)
    artifact = tmp_path / "rendered_timing.json"
    payload = json.loads(artifact.read_text(encoding="utf-8"))
    if change == "source":
        payload["source_sha256"] = "b" * 64
    elif change == "kind":
        payload["kind"] = "translation_review"
    elif change == "failed_check":
        payload["checks"][0]["passed"] = False
    elif change == "no_checks":
        payload["checks"] = []
    else:
        payload = {"passed": True}
        checked["rendered_timing"]["passed"] = True
    artifact.write_text(json.dumps(payload), encoding="utf-8")
    checked["rendered_timing"]["artifact_sha256"] = hashlib.sha256(artifact.read_bytes()).hexdigest()
    report = compare_local_vs_cloud_deliverables(**args, matching_mode="source_alignment", acceptance_evidence=checked)
    assert not report["passed"]
    assert report["dimensions"]["subtitle_integrity"]["rendered_timing"]["validation_errors"]
