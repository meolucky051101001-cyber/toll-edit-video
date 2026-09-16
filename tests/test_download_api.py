import pytest
from fastapi.testclient import TestClient

from backend.app.main import app
from backend.core.database import get_db
from backend.models.download_job import DownloadJob
from backend.models.video import Video
from backend.services.download.service import DownloadService


@pytest.fixture
def test_client(sessions, tmp_path, monkeypatch):
    dl_dir = tmp_path / "downloads"
    dl_dir.mkdir()
    monkeypatch.setattr("backend.core.config.settings.download_dir", dl_dir)

    def override():
        with sessions() as db:
            yield db

    app.dependency_overrides[get_db] = override
    download_svc = DownloadService(sessions, download_dir=dl_dir)
    app.state.download = download_svc

    with TestClient(app) as client:
        yield client, dl_dir, sessions

    app.dependency_overrides.clear()


def test_download_api_flow(test_client):
    client, dl_dir, sessions = test_client

    # Create real video in DB
    with sessions() as db:
        v = Video(
            id="vid-api-1",
            platform="xiaohongshu",
            platform_video_id="66d3cb2b00000000200399cc",
            url="https://www.xiaohongshu.com/explore/66d3cb2b00000000200399cc",
            canonical_url="https://www.xiaohongshu.com/explore/66d3cb2b00000000200399cc",
            fingerprint="fp_api_1",
            search_query="test",
            search_job_id="job1",
            is_mock=False,
        )
        db.add(v)
        db.commit()

    # 1. Request download
    res = client.post("/api/videos/vid-api-1/downloads")
    assert res.status_code == 202
    data = res.json()
    assert data["video_id"] == "vid-api-1"
    download_id = data["id"]
    assert data["status"] in ("queued", "resolving", "downloading", "failed")

    # 2. Query active download
    res_active = client.get("/api/videos/vid-api-1/downloads/active")
    assert res_active.status_code == 200

    # 3. Query status by ID
    res_status = client.get(f"/api/downloads/{download_id}")
    assert res_status.status_code == 200
    assert res_status.json()["id"] == download_id

    # 4. Cancel
    res_cancel = client.post(f"/api/downloads/{download_id}/cancel")
    assert res_cancel.status_code == 200
    assert res_cancel.json()["status"] in ("cancelled", "failed")


def test_download_file_serving_and_traversal_guard(test_client):
    client, dl_dir, sessions = test_client

    # Create completed dummy video file
    video_filename = "xiaohongshu_sample_12345678.mp4"
    file_path = dl_dir / video_filename
    file_path.write_bytes(b"\x00\x00\x00\x1cftypisom\x00\x00\x02\x00isomiso2mp41" + b"\x00" * 100)

    with sessions() as db:
        v = Video(
            id="vid-api-2",
            platform="xiaohongshu",
            platform_video_id="66d3cb2b00000000200399dd",
            url="https://www.xiaohongshu.com/explore/66d3cb2b00000000200399dd",
            canonical_url="https://www.xiaohongshu.com/explore/66d3cb2b00000000200399dd",
            fingerprint="fp_api_2",
            search_query="test",
            search_job_id="job1",
            is_mock=False,
        )
        db.add(v)
        db.flush()

        job = DownloadJob(
            id="job-comp-1",
            video_id=v.id,
            platform="xiaohongshu",
            status="completed",
            relative_path=video_filename,
            file_size=len(file_path.read_bytes()),
        )
        db.add(job)
        db.commit()

    # Request file
    res = client.get("/api/downloads/job-comp-1/file")
    assert res.status_code == 200
    assert res.headers["content-type"] == "video/mp4"
    assert "attachment" in res.headers.get("content-disposition", "")
    assert len(res.content) == len(file_path.read_bytes())

    # Path traversal attack attempt
    with sessions() as db:
        malicious_job = DownloadJob(
            id="job-malicious",
            video_id=v.id,
            platform="xiaohongshu",
            status="completed",
            relative_path="../../etc/passwd",
            file_size=100,
        )
        db.add(malicious_job)
        db.commit()

    res_hack = client.get("/api/downloads/job-malicious/file")
    assert res_hack.status_code in (403, 404)
