import json

import pytest
from sqlalchemy import func, select

from backend.importers.mediacrawler import (
    ImportProblem,
    ImportRequest,
    metric,
    parse_export,
    timestamp,
)
from backend.models import ImportBatch, ImportOrigin, SearchJob, Video
from backend.services.import_service import commit_import, preview


def xhs(**changes):
    return dict(
        note_id="a" * 24,
        type="video",
        title="Synthetic import fixture",
        desc="Test fixture only",
        time=1720000000000,
        liked_count="1.2万",
        tag_list="开箱,文具",
        source_keyword="文具",
        **changes,
    )


def dy():
    return dict(
        aweme_id="1234567890123456789",
        aweme_type="0",
        title="Synthetic Douyin fixture",
        create_time=1720000000,
        liked_count="None",
        collected_count="0",
    )


def parse(rows):
    return parse_export(ImportRequest(content=json.dumps(rows)))


def test_export_mapping_missing_values_and_secret_discard():
    item = xhs()
    item.update(
        note_url="http://127.0.0.1/private?xsec_token=secret-token",
        xsec_token="secret-token",
        cookies="private-cookie",
        image_list="https://localhost/private",
        creator_hash="anonymous-hash",
    )
    output = parse([item, dy()])
    first, second = [i.video for i in output.items]
    assert first.like_count == 12000 and first.hashtags == ["开箱", "文具"]
    assert first.published_at == second.published_at
    assert first.thumbnail_url is None and first.author_id is None
    assert str(first.url) == "https://www.xiaohongshu.com/explore/" + "a" * 24
    assert (
        first.duration_seconds is None and second.like_count is None and second.favorite_count == 0
    )
    assert not first.is_mock
    assert all(
        secret not in output.model_dump_json()
        for secret in ["secret-token", "private-cookie", "anonymous-hash", "127.0.0.1"]
    )


def test_photo_comment_unknown_and_duplicate_are_skipped():
    good = xhs()
    photo = good | {"type": "normal"}
    comment = good | {"comment_id": "x", "content": "comment"}
    unknown = dy() | {"aweme_type": "68"}
    output = parse([good, photo, comment, unknown, good, {"bad": True}])
    assert len(output.items) == 1 and len(output.issues) == 5
    assert output.total == 6


@pytest.mark.parametrize(
    "content,format",
    [
        ("{", "json"),
        ('{"x": 1}', "json"),
        ("[]", "json"),
        ("{}\n{", "jsonl"),
        (json.dumps([{}] * 201), "json"),
        ('["' + "界" * 400000 + '"]', "json"),
    ],
    ids=["invalid", "object", "empty", "bad-jsonl", "too-many", "too-large-utf8"],
)
def test_bad_or_over_limit_file_rejected(content, format):
    with pytest.raises(ImportProblem):
        parse_export(ImportRequest(content=content, format=format))


def test_jsonl_bom_and_id_precision():
    output = parse_export(
        ImportRequest(
            content="\ufeff" + json.dumps(xhs()) + "\n" + json.dumps(dy()), format="jsonl"
        )
    )
    assert len(output.items) == 2
    invalid = dy() | {"aweme_id": float(1234567890123456789)}
    assert not parse([invalid]).items
    invalid = xhs() | {"note_id": "../path"}
    assert not parse([invalid]).items


def test_unknown_metrics_and_time_remain_null():
    assert all(metric(value) is None for value in [None, "None", "赞", "", -1, float("inf"), True])
    assert metric("0") == 0 and metric("2.5k") == 2500
    assert all(timestamp(value) is None for value in [None, "NaN", -1, True, 0])


def test_preview_then_import_repeat_preserves_user_state(sessions):
    parsed = parse([xhs(), dy()])
    with sessions() as db:
        assert preview(parsed, db)["new"] == 2
        assert db.scalar(select(func.count()).select_from(Video)) == 0
        first = commit_import(parsed, db)
        video = db.scalar(select(Video).where(Video.platform == "xiaohongshu"))
        video.status = "saved"
        video.title = "User's existing metadata"
        db.commit()
        again = commit_import(parsed, db)
        assert again["new"] == 0 and again["existing"] == 2
        assert db.scalar(select(func.count()).select_from(Video)) == 2
        assert video.status == "saved" and video.title == "User's existing metadata"
        assert db.get(ImportOrigin, video.id).batch_id == first["job_id"]
        assert db.get(ImportBatch, again["job_id"]).skipped_rows == 0
        assert db.get(SearchJob, first["job_id"]).is_mock is False


def test_import_api_and_provenance(client, sessions):
    body = {"content": json.dumps([xhs(), dy()]), "format": "json"}
    before = client.post("/api/imports/mediacrawler/preview", json=body)
    assert before.status_code == 200 and before.json()["accepted"] == 2
    result = client.post("/api/imports/mediacrawler/commit", json=body)
    assert result.status_code == 201
    job_id = result.json()["job_id"]
    video = client.get(f"/api/videos?job_id={job_id}").json()["items"][0]
    assert video["imported_from"] == "mediacrawler" and not video["is_mock"]
    assert client.get(f"/api/videos/{video['id']}").json()["imported_from"] == "mediacrawler"
    assert client.post(f"/api/videos/{video['id']}/save").json()["status"] == "saved"
    job = client.get(f"/api/search/jobs/{job_id}").json()
    assert job["import_source"] == "mediacrawler"
    rejected = client.post(
        "/api/imports/mediacrawler/commit", json={"content": "secret-token-bad-json"}
    )
    assert rejected.status_code == 422 and "secret-token" not in rejected.text
    empty = client.post(
        "/api/imports/mediacrawler/commit", json={"content": json.dumps([{"comment_id": "x"}])}
    )
    assert empty.status_code == 422
    oversized = client.post("/api/imports/mediacrawler/preview", content=b"x" * 2097153)
    assert oversized.status_code == 413
    forbidden = client.post(
        "/api/imports/mediacrawler/commit", json=body, headers={"origin": "https://example.invalid"}
    )
    assert forbidden.status_code == 403
