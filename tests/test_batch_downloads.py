import pytest
from backend.models.video import Video


def make_video(vid_id, platform, pvid, url, title, duration=None, is_mock=False):
    return Video(
        id=vid_id,
        platform=platform,
        platform_video_id=pvid,
        url=url,
        canonical_url=url,
        fingerprint=f"fp_{vid_id}",
        search_query="test",
        search_job_id="job1",
        title=title,
        duration_seconds=duration,
        is_mock=is_mock,
    )


def test_batch_download_enqueues_valid_videos(client, sessions):
    with sessions() as db:
        v1 = make_video(
            "vid-1",
            "douyin",
            "dy_test_batch_1",
            "https://www.douyin.com/video/1000000000000000001",
            "Video Batch 1",
        )
        v2 = make_video(
            "vid-2",
            "xiaohongshu",
            "xhs_test_batch_2",
            "https://www.rednote.com/explore/0123456789abcdef01234567",
            "Video Batch 2",
        )
        db.add_all([v1, v2])
        db.commit()

    resp = client.post("/api/downloads/batch", json={"video_ids": ["vid-1", "vid-2"]})
    assert resp.status_code == 202
    data = resp.json()
    assert len(data) == 2
    assert {job["video_id"] for job in data} == {"vid-1", "vid-2"}
    assert all(job["status"] == "queued" for job in data)


def test_batch_download_skips_mock_or_invalid_videos(client, sessions):
    with sessions() as db:
        mock_vid = make_video(
            "vid-mock",
            "douyin",
            "mock_dy_1",
            "https://example.invalid/douyin/mock",
            "Mock Video",
            is_mock=True,
        )
        db.add(mock_vid)
        db.commit()

    resp = client.post("/api/downloads/batch", json={"video_ids": ["vid-mock", "non_existent_id"]})
    assert resp.status_code == 202
    assert resp.json() == []


def test_videos_api_duration_filters(client, sessions):
    with sessions() as db:
        short_v = make_video(
            "vid-short",
            "douyin",
            "dy_short",
            "https://www.douyin.com/video/1000000000000000002",
            "Short Video 15s",
            duration=15,
        )
        med_v = make_video(
            "vid-med",
            "douyin",
            "dy_med",
            "https://www.douyin.com/video/1000000000000000003",
            "Medium Video 45s",
            duration=45,
        )
        long_v = make_video(
            "vid-long",
            "douyin",
            "dy_long",
            "https://www.douyin.com/video/1000000000000000004",
            "Long Video 90s",
            duration=90,
        )
        db.add_all([short_v, med_v, long_v])
        db.commit()

    # Short: <= 30s
    res_short = client.get("/api/videos?max_duration=30").json()
    short_titles = [item["title"] for item in res_short["items"]]
    assert "Short Video 15s" in short_titles
    assert "Medium Video 45s" not in short_titles
    assert "Long Video 90s" not in short_titles

    # Medium: 30s - 60s
    res_med = client.get("/api/videos?min_duration=30&max_duration=60").json()
    med_titles = [item["title"] for item in res_med["items"]]
    assert "Medium Video 45s" in med_titles
    assert "Short Video 15s" not in med_titles
    assert "Long Video 90s" not in med_titles

    # Long: >= 60s
    res_long = client.get("/api/videos?min_duration=60").json()
    long_titles = [item["title"] for item in res_long["items"]]
    assert "Long Video 90s" in long_titles
    assert "Short Video 15s" not in long_titles
    assert "Medium Video 45s" not in long_titles
