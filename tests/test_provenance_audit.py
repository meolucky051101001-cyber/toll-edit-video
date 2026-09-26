"""Tests for strict provenance audit and schema validation with independent capture manifest."""


from backend.services.identity_policy import (
    SampleProvenanceResult,
    audit_sample_provenance,
    create_capture_manifest_record,
)


def _make_valid_douyin_sample() -> dict:
    return {
        "platform": "douyin",
        "platform_video_id": "7350000000000000001",
        "url": "https://www.douyin.com/video/7350000000000000001",
        "author": "手账咕卡研究所",
        "collected_at": "2026-09-22T10:00:00Z",
        "title": "手账咕卡贴纸专用镊子开箱测评",
        "like_count": 48200,
        "media_type": "video",
        "ground_truth_relevance": True,
        "rationale": "Relevant video reviewing dedicated sticker tweezers.",
        "query": "nhíp gắp sticker",
        "submitted_query": "贴纸镊子",
        "session_id": "crawled-eval-20260922-01",
        "collector_version": "v1.2.0-provenance",
        "provenance_source": "crawled_benchmark_samples",
        "provenance_level": "verified_strict_audit",
    }


def _make_valid_xhs_sample() -> dict:
    return {
        "platform": "xiaohongshu",
        "platform_video_id": "65e9abcdef0123456789abcd",
        "url": "https://www.xiaohongshu.com/explore/65e9abcdef0123456789abcd",
        "author": "咕卡好物分享",
        "collected_at": "2026-09-22T10:15:00+08:00",
        "title": "手账贴纸镊子超好用分享",
        "like_count": 12000,
        "media_type": "video",
        "ground_truth_relevance": True,
        "rationale": "Relevant craft video reviewing sticker tweezers on Xiaohongshu.",
        "query": "nhíp gắp sticker",
        "submitted_query": "贴纸镊子",
        "session_id": "crawled-eval-20260922-02",
        "collector_version": "v1.2.0-provenance",
        "provenance_source": "crawled_benchmark_samples",
        "provenance_level": "verified_strict_audit",
    }


def test_provenance_audit_matching_manifest_verified():
    """Sample with matching independent capture manifest record and matching hash is verified strictly."""
    dy = _make_valid_douyin_sample()
    manifest_record = create_capture_manifest_record(dy)
    res_dy = audit_sample_provenance(dy, expected_query="nhíp gắp sticker", capture_manifest=[manifest_record])
    assert isinstance(res_dy, SampleProvenanceResult)
    assert res_dy.schema_valid is True
    assert res_dy.source_provenance_verified is True
    assert res_dy.status == "VERIFIED_STRICT"
    assert manifest_record["record_id"] in res_dy.detail

    # Backward compatibility tuple unpacking (schema_valid, detail)
    ok, msg = res_dy
    assert ok is True
    assert "strictly verified" in msg

    xhs = _make_valid_xhs_sample()
    xhs_manifest = create_capture_manifest_record(xhs)
    res_xhs = audit_sample_provenance(xhs, expected_query="nhíp gắp sticker", capture_manifest={"records": [xhs_manifest]})
    assert res_xhs.schema_valid is True
    assert res_xhs.source_provenance_verified is True
    assert res_xhs.status == "VERIFIED_STRICT"


def test_provenance_audit_rejects_fake_session_id_with_valid_format_when_no_manifest():
    """Properly formatted session_id is NOT proof of provenance without an independent manifest."""
    s = _make_valid_douyin_sample()
    s["session_id"] = "session-fake-valid-format-20260924"
    res = audit_sample_provenance(s, capture_manifest=None)
    assert res.schema_valid is True
    assert res.source_provenance_verified is False
    assert res.status == "UNVERIFIED_PENDING_CAPTURE"
    assert "lacks matching independent capture manifest" in res.detail


def test_provenance_audit_rejects_arbitrary_metadata_hash_without_manifest():
    """Arbitrary hash present on the sample itself is NOT proof without an independent manifest."""
    s = _make_valid_douyin_sample()
    s["metadata_hash"] = "sha256:112233445566778899aabbccddeeff00112233445566778899aabbccddeeff00"
    res = audit_sample_provenance(s, capture_manifest=None)
    assert res.schema_valid is True
    assert res.source_provenance_verified is False
    assert res.status == "UNVERIFIED_PENDING_CAPTURE"


def test_provenance_audit_rejects_nonexistent_capture_id_in_manifest():
    """Rejects verification when sample references a capture ID that does not exist in the manifest."""
    s = _make_valid_douyin_sample()
    s["capture_id"] = "cap-nonexistent-999"
    other_sample = _make_valid_xhs_sample()
    other_manifest = [create_capture_manifest_record(other_sample)]
    res = audit_sample_provenance(s, capture_manifest=other_manifest)
    assert res.schema_valid is True
    assert res.source_provenance_verified is False
    assert res.status == "UNVERIFIED_PENDING_CAPTURE"


def test_provenance_audit_rejects_mismatched_metadata_hash_in_manifest():
    """Rejects verification when manifest record exists but metadata hash does not match computed hash."""
    s = _make_valid_douyin_sample()
    manifest_record = create_capture_manifest_record(s)
    manifest_record["metadata_hash"] = "sha256:0000000000000000000000000000000000000000000000000000000000000000"
    res = audit_sample_provenance(s, capture_manifest=[manifest_record])
    assert res.schema_valid is True
    assert res.source_provenance_verified is False
    assert res.status == "UNVERIFIED_HASH_MISMATCH"
    assert "Metadata hash mismatch" in res.detail


def test_provenance_audit_legacy_unverified_marked_unverified():
    """Legacy unverified samples must NEVER count as source_provenance_verified even if manifest exists."""
    s = _make_valid_douyin_sample()
    s["provenance_level"] = "legacy_unverified"
    manifest_record = create_capture_manifest_record(s)
    res = audit_sample_provenance(s, capture_manifest=[manifest_record])
    assert res.schema_valid is True
    assert res.source_provenance_verified is False
    assert res.status == "UNVERIFIED_LEGACY"
    assert "Legacy sample without verified capture source" in res.detail


def test_provenance_audit_rejects_spoofed_and_insecure_hostnames():
    """Rejects spoofed hostnames, attacker subdomains, and non-HTTPS schemes."""
    spoofed_urls = [
        "https://douyin.com.attacker.test/video/7350000000000000001",
        "https://www.douyin.com.fake.site/video/7350000000000000001",
        "https://evil-douyin.com/video/7350000000000000001",
        "https://attacker.com/douyin.com/video/7350000000000000001",
        "http://www.douyin.com/video/7350000000000000001",  # Insecure http
        "https://xiaohongshu.com.phishing.org/explore/65e9abcdef0123456789abcd",
        "https://rednote.com.fake.site/explore/65e9abcdef0123456789abcd",
    ]

    for bad_url in spoofed_urls:
        s = _make_valid_douyin_sample() if "douyin" in bad_url else _make_valid_xhs_sample()
        s["url"] = bad_url
        res = audit_sample_provenance(s)
        assert res.schema_valid is False, f"Expected rejection for spoofed URL: {bad_url}"
        assert res.source_provenance_verified is False
        assert res.status == "INVALID_SCHEMA"


def test_provenance_audit_rejects_url_id_mismatch():
    """Rejects samples where URL ID does not match platform_video_id."""
    s = _make_valid_douyin_sample()
    s["platform_video_id"] = "7350000000000000099"
    res = audit_sample_provenance(s)
    assert res.schema_valid is False
    assert res.status == "INVALID_SCHEMA"
    assert "does not match" in res.detail


def test_provenance_audit_rejects_invalid_and_naive_timestamps():
    """Rejects timestamps without timezones or with invalid ISO 8601 formatting."""
    bad_timestamps = [
        "2026-09-22 10:00:00",  # No ISO 'T' and no tz
        "2026-09-22T10:00:00",  # ISO format but naive (no timezone offset or Z)
        "22-09-2026T10:00:00Z",  # Non-standard date format
        "not_a_timestamp",
        "",
    ]

    for bad_ts in bad_timestamps:
        s = _make_valid_douyin_sample()
        s["collected_at"] = bad_ts
        res = audit_sample_provenance(s)
        assert res.schema_valid is False, f"Expected rejection for timestamp '{bad_ts}'"
        assert res.status == "INVALID_SCHEMA"


def test_provenance_audit_rejects_missing_metadata():
    """Rejects strict verification when any required metadata field is missing."""
    req_fields = [
        "query",
        "submitted_query",
        "session_id",
        "collector_version",
        "provenance_source",
        "provenance_level",
    ]

    for f in req_fields:
        s = _make_valid_douyin_sample()
        del s[f]
        res = audit_sample_provenance(s)
        assert res.schema_valid is True  # Base contract is valid
        assert res.source_provenance_verified is False  # Provenance metadata incomplete
        assert res.status == "UNVERIFIED_MISSING_METADATA"
        assert f in res.detail


def test_provenance_audit_rejects_query_mismatch():
    """Rejects samples where query metadata does not match outer group expected query."""
    s = _make_valid_douyin_sample()
    s["query"] = "nhíp gắp sticker"
    res = audit_sample_provenance(s, expected_query="bàn phím cơ giá rẻ")
    assert res.schema_valid is True
    assert res.source_provenance_verified is False
    assert res.status == "UNVERIFIED"
    assert "does not match" in res.detail


def test_provenance_audit_with_static_fixture_and_precomputed_hash():
    """Manifest record with precomputed static hash created independently verifies strictly."""
    static_sample = {
        "platform": "douyin",
        "platform_video_id": "7350000000000000001",
        "url": "https://www.douyin.com/video/7350000000000000001",
        "title": "手账咕卡贴纸专用镊子开箱测评",
        "author": "手账咕卡研究所",
        "caption": "手账咕卡贴纸专用镊子开箱测评 弯头直头对比",
        "hashtags": ["贴纸", "镊子", "咕卡"],
        "media_type": "video",
        "like_count": 48200,
        "collected_at": "2026-09-22T10:00:00Z",
        "query": "nhíp gắp sticker",
        "submitted_query": "贴纸镊子",
        "session_id": "session-fixed-20260926-01",
        "collector_version": "v1.2.0-live",
        "provenance_source": "douyin_live_search_capture",
        "provenance_level": "verified_strict_audit",
        "ground_truth_relevance": True,
        "rationale": "Relevant video reviewing dedicated sticker tweezers.",
    }

    # Manifest record with precomputed hash (calculated independently beforehand without using helper in test)
    static_manifest = [
        {
            "record_id": "cap-douyin-7350000000000000001",
            "platform": "douyin",
            "platform_video_id": "7350000000000000001",
            "url": "https://www.douyin.com/video/7350000000000000001",
            "query": "nhíp gắp sticker",
            "submitted_query": "贴纸镊子",
            "collected_at": "2026-09-22T10:00:00Z",
            "session_id": "session-fixed-20260926-01",
            "collector_version": "v1.2.0-live",
            "author": "手账咕卡研究所",
            "title": "手账咕卡贴纸专用镊子开箱测评",
            "caption": "手账咕卡贴纸专用镊子开箱测评 弯头直头对比",
            "hashtags": ["贴纸", "镊子", "咕卡"],
            "media_type": "video",
            "like_count": 48200,
            "hash_version": "v2",
            "metadata_hash": "sha256:5531bd8765d43ab66a4c64bfc762fb4bfbe4edebe889ca9d517f4833f9ebc775",
        }
    ]

    res = audit_sample_provenance(
        static_sample,
        expected_query="nhíp gắp sticker",
        capture_manifest=static_manifest,
    )
    assert res.schema_valid is True
    assert res.source_provenance_verified is True
    assert res.status == "VERIFIED_STRICT"
    assert res.manifest_record_id == "cap-douyin-7350000000000000001"


def test_provenance_audit_tampering_each_hashed_field_fails_verification():
    """Mutating ANY single field included in metadata hash must fail verification."""
    base_sample = {
        "platform": "douyin",
        "platform_video_id": "7350000000000000001",
        "url": "https://www.douyin.com/video/7350000000000000001",
        "title": "手账咕卡贴纸专用镊子开箱测评",
        "author": "手账咕卡研究所",
        "caption": "手账咕卡贴纸专用镊子开箱测评 弯头直头对比",
        "hashtags": ["贴纸", "镊子", "咕卡"],
        "media_type": "video",
        "like_count": 48200,
        "collected_at": "2026-09-22T10:00:00Z",
        "query": "nhíp gắp sticker",
        "submitted_query": "贴纸镊子",
        "session_id": "session-fixed-20260926-01",
        "collector_version": "v1.2.0-live",
        "provenance_source": "douyin_live_search_capture",
        "provenance_level": "verified_strict_audit",
        "ground_truth_relevance": True,
        "rationale": "Relevant video reviewing dedicated sticker tweezers.",
    }

    manifest = [
        {
            "record_id": "cap-douyin-7350000000000000001",
            "platform": "douyin",
            "platform_video_id": "7350000000000000001",
            "url": "https://www.douyin.com/video/7350000000000000001",
            "query": "nhíp gắp sticker",
            "submitted_query": "贴纸镊子",
            "collected_at": "2026-09-22T10:00:00Z",
            "session_id": "session-fixed-20260926-01",
            "collector_version": "v1.2.0-live",
            "author": "手账咕卡研究所",
            "title": "手账咕卡贴纸专用镊子开箱测评",
            "caption": "手账咕卡贴纸专用镊子开箱测评 弯头直头对比",
            "hashtags": ["贴纸", "镊子", "咕卡"],
            "media_type": "video",
            "like_count": 48200,
            "hash_version": "v2",
            "metadata_hash": "sha256:5531bd8765d43ab66a4c64bfc762fb4bfbe4edebe889ca9d517f4833f9ebc775",
        }
    ]

    field_mutations = {
        "title": "Tiêu đề bị chỉnh sửa",
        "author": "Kênh giả mạo",
        "caption": "Caption quảng cáo mới",
        "hashtags": ["spam_tag"],
        "like_count": 999999,
        "collected_at": "2026-09-22T10:05:00Z",
        "submitted_query": "truy vấn sai",
        "session_id": "session-tampered-99",
        "collector_version": "v9.9.9-modified",
    }

    for field_name, bad_value in field_mutations.items():
        tampered_sample = dict(base_sample)
        tampered_sample[field_name] = bad_value
        res = audit_sample_provenance(
            tampered_sample,
            expected_query="nhíp gắp sticker",
            capture_manifest=manifest,
        )
        assert res.source_provenance_verified is False, f"Expected verification failure when tampering '{field_name}'"
        assert res.status == "UNVERIFIED_HASH_MISMATCH"


def test_annotation_hash_and_tamper_detection():
    """Separation of crawl data and benchmark annotations with verifiable annotation hash."""
    from backend.services.identity_policy import (
        audit_sample_annotation,
        compute_sample_annotation_hash,
        create_annotation_record,
    )

    sample = _make_valid_douyin_sample()

    # Precomputed independently for: annotation_version=v1, ground_truth_relevance=True, platform=douyin,
    # platform_video_id=7350000000000000001, rationale=Relevant video reviewing dedicated sticker tweezers.,
    # reviewed_at=2026-09-22T10:00:00Z, reviewer=benchmark_reviewer
    expected_ann_hash = "sha256:f3ada76cb11cb1f65a93fe236fbc8c170baaa1aa9fd24f59cbf5388bb0f966fd"
    ann_rec = {
        "record_id": "ann-douyin-7350000000000000001",
        "platform": "douyin",
        "platform_video_id": "7350000000000000001",
        "ground_truth_relevance": True,
        "rationale": "Relevant video reviewing dedicated sticker tweezers.",
        "reviewer": "benchmark_reviewer",
        "reviewed_at": "2026-09-22T10:00:00Z",
        "annotation_version": "v1",
        "annotation_hash": expected_ann_hash,
    }

    assert compute_sample_annotation_hash(ann_rec) == expected_ann_hash

    # Verify create_annotation_record helper produces matching hash
    ann_created = create_annotation_record(sample, reviewer="benchmark_reviewer", reviewed_at="2026-09-22T10:00:00Z")
    assert ann_created["annotation_hash"] == expected_ann_hash

    # Audit passes with valid record
    ok, msg = audit_sample_annotation(sample, ann_rec)
    assert ok is True
    assert "Annotation verified" in msg

    # Mutating ground_truth_relevance in sample or record fails
    sample_tampered = dict(sample, ground_truth_relevance=False)
    ok_rel, msg_rel = audit_sample_annotation(sample_tampered, ann_rec)
    assert ok_rel is False
    assert "Annotation relevance mismatch" in msg_rel

    # Mutating rationale fails
    sample_tampered_rat = dict(sample, rationale="Modified rationale that does not match")
    ok_rat, msg_rat = audit_sample_annotation(sample_tampered_rat, ann_rec)
    assert ok_rat is False
    assert "Annotation rationale mismatch" in msg_rat

    # Full audit integration with annotation_manifest
    manifest = [create_capture_manifest_record(sample)]
    res = audit_sample_provenance(
        sample,
        expected_query="nhíp gắp sticker",
        capture_manifest=manifest,
        annotation_manifest=[ann_rec],
    )
    assert res.source_provenance_verified is True
    assert res.annotation_verified is True

    # Audit fails if annotation record is tampered with
    ann_tampered = dict(ann_rec, ground_truth_relevance=False)
    res_tampered = audit_sample_provenance(
        sample,
        expected_query="nhíp gắp sticker",
        capture_manifest=manifest,
        annotation_manifest=[ann_tampered],
    )
    assert res_tampered.source_provenance_verified is False
    assert res_tampered.status == "UNVERIFIED_ANNOTATION_MISMATCH"


def test_live_crawl_manifest_append_and_lookup(tmp_path):
    """Verifies that live crawl captures are appended to an independent JSONL manifest and can be audited."""
    from backend.services.identity_policy import (
        load_live_capture_manifest,
        lookup_capture_manifest_record,
        record_live_crawl_capture,
    )

    test_manifest_file = tmp_path / "test_live_manifest.jsonl"
    sample = _make_valid_douyin_sample()

    # Ingest a real live crawl capture
    rec = record_live_crawl_capture(
        sample,
        query="nhíp gắp sticker",
        submitted_query="贴纸镊子",
        session_id="test-live-run-001",
        collector_version="v1.2.0-live",
        manifest_path=test_manifest_file,
    )

    assert test_manifest_file.exists()
    records = load_live_capture_manifest(test_manifest_file)
    assert len(records) == 1
    assert records[0]["platform_video_id"] == sample["platform_video_id"]
    assert records[0]["metadata_hash"] == rec["metadata_hash"]

    # Lookup matches from JSONL path directly
    found = lookup_capture_manifest_record(test_manifest_file, sample)
    assert found is not None
    assert found["record_id"] == rec["record_id"]

    # Audit sample using JSONL manifest file path
    sample_to_audit = dict(sample)
    sample_to_audit["session_id"] = "test-live-run-001"
    sample_to_audit["collector_version"] = "v1.2.0-live"
    sample_to_audit["collected_at"] = rec["collected_at"]

    res = audit_sample_provenance(
        sample_to_audit,
        expected_query="nhíp gắp sticker",
        capture_manifest=test_manifest_file,
    )
    assert res.schema_valid is True
    assert res.source_provenance_verified is True
    assert res.status == "VERIFIED_STRICT"

