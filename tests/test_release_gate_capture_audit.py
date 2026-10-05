from backend.services.identity_policy import compute_sample_metadata_hash
from scripts.release_gate import audit_current_run_capture_records, build_manifest_storage_report


def _capture_record():
    record = {
        "record_id": "cap-douyin-7350000000000000001-test",
        "platform": "douyin",
        "platform_video_id": "7350000000000000001",
        "url": "https://www.douyin.com/video/7350000000000000001",
        "query": "nhíp gắp sticker",
        "submitted_query": "贴纸镊子",
        "collected_at": "2026-09-26T10:00:00+00:00",
        "session_id": "release-gate-test",
        "collector_version": "test-live",
        "author": "author",
        "title": "贴纸镊子视频",
        "caption": "贴纸镊子",
        "hashtags": ["贴纸", "镊子"],
        "media_type": "video",
        "like_count": 5,
        "hash_version": "v2",
    }
    record["metadata_hash"] = compute_sample_metadata_hash(record, version="v2")
    return record


def test_blocked_live_platform_does_not_count_historical_or_unverified_rows():
    result = audit_current_run_capture_records(
        [_capture_record()],
        {"douyin": {"status": "BLOCKED (CHALLENGE_REQUIRED)", "retrieval_available": False, "results_count": 0}},
    )
    assert result["current_run_live_captures"] == 0
    assert result["current_run_unverified_capture_records"] == 1
    assert result["all_retrieved_results_verified"] is False


def test_current_run_count_requires_complete_valid_v2_provenance():
    record = _capture_record()
    passed = audit_current_run_capture_records(
        [record],
        {"douyin": {"status": "PASS", "retrieval_available": True, "results_count": 1}},
    )
    assert passed["current_run_live_captures"] == 1
    assert passed["current_run_unverified_capture_records"] == 0
    assert passed["all_retrieved_results_verified"] is True

    tampered = dict(record, metadata_hash="sha256:bad")
    rejected = audit_current_run_capture_records(
        [tampered],
        {"douyin": {"status": "PASS", "retrieval_available": True, "results_count": 1}},
    )
    assert rejected["current_run_live_captures"] == 0
    assert rejected["current_run_unverified_capture_records"] == 1
    assert rejected["all_retrieved_results_verified"] is False


def test_release_report_separates_historical_rows_from_current_live_captures():
    historical = [
        {"platform_video_id": "pvid_fixture_1", "url": "https://www.rednote.com/explore/fake"},
        {"platform_video_id": "7350000000000000001", "url": "https://www.douyin.com/video/7350000000000000001"},
    ]
    storage = build_manifest_storage_report("data/manifests/live_capture_manifest.jsonl", historical)
    blocked = audit_current_run_capture_records(
        [],
        {"douyin": {"status": "BLOCKED (CHALLENGE_REQUIRED)", "retrieval_available": False, "results_count": 0}},
    )
    storage["current_run_live_captures"] = blocked["current_run_live_captures"]
    assert storage["historical_records_count"] == 2
    assert storage["historical_unverified_records_count"] == 2
    assert storage["historical_suspected_synthetic_records_count"] == 1
    assert storage["historical_quarantined_records_count"] == 1
    assert storage["historical_quarantine_mode"] == "logical_only_read_only"
    assert storage["current_run_live_captures"] == 0
    assert "live_records_count" not in storage
