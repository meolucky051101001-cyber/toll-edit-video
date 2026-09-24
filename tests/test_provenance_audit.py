"""Tests for strict provenance audit and schema validation."""


from backend.services.identity_policy import (
    SampleProvenanceResult,
    audit_sample_provenance,
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


def test_provenance_audit_valid_samples():
    """Valid samples on both platforms must pass both schema validation and strict source provenance."""
    dy = _make_valid_douyin_sample()
    res_dy = audit_sample_provenance(dy, expected_query="nhíp gắp sticker")
    assert isinstance(res_dy, SampleProvenanceResult)
    assert res_dy.schema_valid is True
    assert res_dy.source_provenance_verified is True
    assert res_dy.status == "VERIFIED_STRICT"

    # Backward compatibility tuple unpacking
    ok, msg = res_dy
    assert ok is True
    assert "strictly verified" in msg

    xhs = _make_valid_xhs_sample()
    res_xhs = audit_sample_provenance(xhs, expected_query="nhíp gắp sticker")
    assert res_xhs.schema_valid is True
    assert res_xhs.source_provenance_verified is True
    assert res_xhs.status == "VERIFIED_STRICT"


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


def test_provenance_audit_legacy_unverified_marked_unverified():
    """Legacy unverified samples must NEVER count as source_provenance_verified."""
    s = _make_valid_douyin_sample()
    s["provenance_level"] = "legacy_unverified"
    res = audit_sample_provenance(s)
    assert res.schema_valid is True
    assert res.source_provenance_verified is False
    assert res.status == "UNVERIFIED_LEGACY"
    assert "Legacy sample without verified capture source" in res.detail


def test_provenance_audit_insufficient_source_evidence():
    """Rejects samples with placeholder or invalid session_id when capture ref is absent."""
    s = _make_valid_douyin_sample()
    s["session_id"] = "placeholder"
    res = audit_sample_provenance(s)
    assert res.schema_valid is True
    assert res.source_provenance_verified is False
    assert res.status == "UNVERIFIED_INSUFFICIENT_EVIDENCE"
