import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend.core.database import Base
from backend.models.download_job import DownloadJob
from backend.models.video import Video
from backend.schemas.downloads import ResolvedMedia
from backend.services.download.base import BaseDownloadAdapter
from backend.services.download.service import DownloadService, is_safe_media_url, is_valid_mp4


class FakeDownloadAdapter(BaseDownloadAdapter):
    def __init__(self, platform="xiaohongshu", media_url="https://cdn.example.com/video.mp4"):
        self._platform = platform
        self._media_url = media_url

    @property
    def name(self) -> str:
        return "FakeAdapter"

    @property
    def version(self) -> str:
        return "fake:1.0"

    async def health_check(self) -> bool:
        return True

    async def resolve(self, video: Video) -> ResolvedMedia:
        return ResolvedMedia(
            platform=video.platform,  # type: ignore[arg-type]
            video_id=video.platform_video_id or "vid",
            media_url=self._media_url,
            headers={"User-Agent": "TestUA"},
            format="mp4",
            adapter_name=self.name,
            adapter_version=self.version,
        )


@pytest.fixture
def download_test_env(tmp_path, monkeypatch):
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine, expire_on_commit=False)

    dl_dir = tmp_path / "downloads"
    dl_dir.mkdir()
    monkeypatch.setattr("backend.services.download.service.settings.download_dir", dl_dir)

    adapter = FakeDownloadAdapter()
    service = DownloadService(sessions, xhs_adapter=adapter, douyin_adapter=adapter)
    service.download_dir = dl_dir
    service.temp_dir = dl_dir / ".temp"
    service.temp_dir.mkdir(exist_ok=True)

    yield sessions, service, dl_dir


def test_ssrf_validation(monkeypatch):
    assert is_safe_media_url("https://sns-video-bd.xhscdn.com/video.mp4")
    assert is_safe_media_url("https://aweme.snssdk.com/video.mp4")

    # Insecure schemes
    assert not is_safe_media_url("http://sns-video-bd.xhscdn.com/video.mp4")
    assert not is_safe_media_url("ftp://sns-video-bd.xhscdn.com/video.mp4")

    # Private and loopback addresses
    assert not is_safe_media_url("https://localhost/video.mp4")
    assert not is_safe_media_url("https://127.0.0.1/video.mp4")
    assert not is_safe_media_url("https://10.0.0.5/video.mp4")
    assert not is_safe_media_url("https://192.168.1.100/video.mp4")
    assert not is_safe_media_url("https://172.16.0.1/video.mp4")
    assert not is_safe_media_url("https://169.254.169.254/latest/meta-data/")

    # Non-standard ports
    assert not is_safe_media_url("https://example.com:8443/video.mp4")

    # Hostname that resolves to private IP (DNS rebinding / SSRF)
    def mock_getaddrinfo_private(host, port, **kwargs):
        import socket
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", port))]

    monkeypatch.setattr("socket.getaddrinfo", mock_getaddrinfo_private)
    assert not is_safe_media_url("https://evil-internal.example.com/video.mp4")

    # Hostname that fails DNS
    def mock_getaddrinfo_fail(host, port, **kwargs):
        import socket
        raise socket.gaierror(11001, "getaddrinfo failed")

    monkeypatch.setattr("socket.getaddrinfo", mock_getaddrinfo_fail)
    assert not is_safe_media_url("https://nonexistent-domain-xyz.com/video.mp4")


def test_is_valid_mp4(tmp_path):
    import struct
    from pathlib import Path

    from backend.services.download.validator import (
        parse_iso_bmff_structure,
        probe_media_file_sync,
    )

    # 1. Real MP4 fixture passes both ISO-BMFF parser and ffprobe
    real_mp4 = Path("tests/fixtures/tiny_valid.mp4")
    assert real_mp4.is_file(), "tiny_valid.mp4 fixture must exist"
    assert is_valid_mp4(real_mp4)
    iso_ok, err = parse_iso_bmff_structure(real_mp4)
    assert iso_ok, f"ISO parse error: {err}"
    probe_ok, err = probe_media_file_sync(real_mp4)
    assert probe_ok, f"Probe error: {err}"

    # 2. Fake moov with 'vide' in raw garbage (Review Round 14 probe) must FAIL
    ftyp = struct.pack(">I4s4sI4s", 20, b"ftyp", b"isom", 512, b"isom")
    fake_moov_payload = b"mvhd" + b"\x00" * 20 + b"random_noise_vide_not_a_trak"
    fake_moov = struct.pack(">I4s", len(fake_moov_payload) + 8, b"moov") + fake_moov_payload
    mdat_payload = b"somedata" * 10
    fake_mdat = struct.pack(">I4s", len(mdat_payload) + 8, b"mdat") + mdat_payload

    fake_file = tmp_path / "fake_box.mp4"
    fake_file.write_bytes(ftyp + fake_moov + fake_mdat)
    assert not is_valid_mp4(fake_file)
    fake_iso_ok, fake_err = parse_iso_bmff_structure(fake_file)
    assert not fake_iso_ok

    # 3. Truncated MP4 (mdat box claims size larger than remaining file)
    truncated_file = tmp_path / "truncated.mp4"
    truncated_file.write_bytes(ftyp + fake_moov + struct.pack(">I4s", 1000, b"mdat") + b"short")
    assert not is_valid_mp4(truncated_file)

    # 4. Missing video track in moov
    audio_only_payload = b"mvhd" + b"\x00" * 20 + b"trak" + b"mdia" + b"hdlr" + b"soun"
    audio_moov = struct.pack(">I4s", len(audio_only_payload) + 8, b"moov") + audio_only_payload
    audio_file = tmp_path / "audio_only.mp4"
    audio_file.write_bytes(ftyp + audio_moov + fake_mdat)
    assert not is_valid_mp4(audio_file)

    # 5. HTML error page disguised as mp4
    html_file = tmp_path / "error.mp4"
    html_file.write_bytes(b"<!DOCTYPE html><html><body>Error 403 Forbidden</body></html>")
    assert not is_valid_mp4(html_file)

    # 6. JSON error page disguised as mp4
    json_file = tmp_path / "error.json"
    json_file.write_bytes(b'{"code": 404, "message": "Not Found"}')
    assert not is_valid_mp4(json_file)


def test_missing_ffprobe_fails_verification(monkeypatch):
    import shutil
    from pathlib import Path

    from backend.services.download.validator import probe_media_file_sync

    real_mp4 = Path("tests/fixtures/tiny_valid.mp4")
    monkeypatch.setattr(shutil, "which", lambda cmd: None)

    ok, err = probe_media_file_sync(real_mp4)
    assert not ok
    assert "ffprobe_not_found" in (err or "")
    assert not is_valid_mp4(real_mp4)


@pytest.mark.anyio
async def test_ffprobe_timeout_fails_verification(monkeypatch):
    import asyncio
    from pathlib import Path

    from backend.services.download.validator import probe_media_file_async

    real_mp4 = Path("tests/fixtures/tiny_valid.mp4")

    async def mock_wait_for(fut, timeout):
        raise asyncio.TimeoutError()

    monkeypatch.setattr(asyncio, "wait_for", mock_wait_for)

    ok, err = await probe_media_file_async(real_mp4)
    assert not ok
    assert "ffprobe_timeout" in (err or "")



def test_request_download_deduplication(download_test_env):
    sessions, service, dl_dir = download_test_env

    with sessions() as db:
        v = Video(
            id="vid-real-1",
            platform="xiaohongshu",
            platform_video_id="66d3cb2b00000000200399e3",
            url="https://www.xiaohongshu.com/explore/66d3cb2b00000000200399e3",
            canonical_url="https://www.xiaohongshu.com/explore/66d3cb2b00000000200399e3",
            fingerprint="fp1",
            search_query="test",
            search_job_id="job1",
            is_mock=False,
        )
        db.add(v)
        db.commit()

    # Request 1
    job1 = service.request_download("vid-real-1")
    assert job1.status == "queued"

    # Rapid Request 2 for same video
    job2 = service.request_download("vid-real-1")
    assert job2.id == job1.id  # Deduplicated! Returns same job


def test_request_download_rejects_mock_video(download_test_env):
    sessions, service, dl_dir = download_test_env

    with sessions() as db:
        v = Video(
            id="vid-mock-1",
            platform="xiaohongshu",
            platform_video_id="mock-1",
            url="https://example.invalid/xiaohongshu/mock-1",
            canonical_url="https://example.invalid/xiaohongshu/mock-1",
            fingerprint="fp1",
            search_query="test",
            search_job_id="job1",
            is_mock=True,
        )
        db.add(v)
        db.commit()

    with pytest.raises(ValueError, match="mẫu"):
        service.request_download("vid-mock-1")


def test_cancel_download(download_test_env):
    sessions, service, dl_dir = download_test_env

    with sessions() as db:
        v = Video(
            id="vid-real-2",
            platform="douyin",
            platform_video_id="7123456789012345678",
            url="https://www.douyin.com/video/7123456789012345678",
            canonical_url="https://www.douyin.com/video/7123456789012345678",
            fingerprint="fp2",
            search_query="test",
            search_job_id="job1",
            is_mock=False,
        )
        db.add(v)
        db.commit()

    job = service.request_download("vid-real-2")
    cancelled = service.cancel_download(job.id)
    assert cancelled.status == "cancelled"


def test_recover_interrupted_jobs(download_test_env):
    sessions, service, dl_dir = download_test_env

    with sessions() as db:
        v = Video(
            id="vid-real-3",
            platform="xiaohongshu",
            platform_video_id="66d3cb2b00000000200399aa",
            url="https://www.xiaohongshu.com/explore/66d3cb2b00000000200399aa",
            canonical_url="https://www.xiaohongshu.com/explore/66d3cb2b00000000200399aa",
            fingerprint="fp3",
            search_query="test",
            search_job_id="job1",
            is_mock=False,
        )
        db.add(v)
        db.flush()

        j1 = DownloadJob(id="job-dangle-1", video_id=v.id, platform="xiaohongshu", status="downloading")
        j2 = DownloadJob(id="job-dangle-2", video_id=v.id, platform="xiaohongshu", status="queued")
        db.add_all([j1, j2])
        db.commit()

    recovered = service.recover_interrupted_jobs()
    assert recovered == 2

    with sessions() as db:
        j1_rec = db.get(DownloadJob, "job-dangle-1")
        assert j1_rec.status == "interrupted"
        assert j1_rec.error_code == "server_restarted"


def test_force_redownload(download_test_env):
    sessions, service, dl_dir = download_test_env

    with sessions() as db:
        v = Video(
            id="vid-force-1",
            platform="douyin",
            platform_video_id="7123456789012345678",
            url="https://www.douyin.com/video/7123456789012345678",
            canonical_url="https://www.douyin.com/video/7123456789012345678",
            fingerprint="fp-force",
            search_query="test",
            search_job_id="job1",
            is_mock=False,
        )
        db.add(v)
        db.flush()

        # Completed file exists on disk
        fn = "douyin_7123456789012345678_completed.mp4"
        (dl_dir / fn).write_bytes(b"dummy")

        j_comp = DownloadJob(
            id="job-comp-1",
            video_id=v.id,
            platform="douyin",
            status="completed",
            relative_path=fn,
        )
        db.add(j_comp)
        db.commit()

    # Normal request reuses completed job
    j_reuse = service.request_download("vid-force-1", force=False)
    assert j_reuse.id == "job-comp-1"
    assert j_reuse.status == "completed"

    # Force request creates a fresh queued job
    j_fresh = service.request_download("vid-force-1", force=True)
    assert j_fresh.id != "job-comp-1"
    assert j_fresh.status == "queued"


def test_get_active_job_preserves_terminal_state(download_test_env):
    sessions, service, dl_dir = download_test_env

    with sessions() as db:
        v = Video(
            id="vid-term-1",
            platform="xiaohongshu",
            platform_video_id="66d3cb2b00000000200399bb",
            url="https://www.xiaohongshu.com/explore/66d3cb2b00000000200399bb",
            canonical_url="https://www.xiaohongshu.com/explore/66d3cb2b00000000200399bb",
            fingerprint="fp-term",
            search_query="test",
            search_job_id="job1",
            is_mock=False,
        )
        db.add(v)
        db.flush()

        j_fail = DownloadJob(
            id="job-failed-1",
            video_id=v.id,
            platform="xiaohongshu",
            status="failed",
            error_code="service_unavailable",
            error_message="Dịch vụ XHS chưa khởi động.",
        )
        db.add(j_fail)
        db.commit()

    # On page reload, get_active_job_for_video preserves the failed job state
    active = service.get_active_job_for_video("vid-term-1")
    assert active is not None
    assert active.id == "job-failed-1"
    assert active.status == "failed"
    assert active.error_code == "service_unavailable"


@pytest.mark.anyio
async def test_shutdown_lifecycle(download_test_env):
    import asyncio
    sessions, service, dl_dir = download_test_env

    with sessions() as db:
        v = Video(
            id="vid-shut-1",
            platform="xiaohongshu",
            platform_video_id="66d3cb2b00000000200399cc",
            url="https://www.xiaohongshu.com/explore/66d3cb2b00000000200399cc",
            canonical_url="https://www.xiaohongshu.com/explore/66d3cb2b00000000200399cc",
            fingerprint="fp-shut",
            search_query="test",
            search_job_id="job1",
            is_mock=False,
        )
        db.add(v)
        db.flush()

        j = DownloadJob(id="job-running-shut", video_id=v.id, platform="xiaohongshu", status="downloading")
        db.add(j)
        db.commit()

    # Simulate active background task and .part file
    part_file = service.temp_dir / "job-running-shut.part"
    part_file.write_bytes(b"partial video data")

    async def dummy_task():
        try:
            await asyncio.sleep(10.0)
        except asyncio.CancelledError:
            pass

    t = asyncio.create_task(dummy_task())
    service.active_tasks["job-running-shut"] = t

    await service.shutdown()

    assert t.cancelled() or t.done()
    assert not part_file.exists()  # Cleaned up

    with sessions() as db:
        rec = db.get(DownloadJob, "job-running-shut")
        assert rec.status == "interrupted"
        assert rec.error_code == "server_shutdown"


@pytest.mark.anyio
async def test_download_stream_blocks_redirect_to_private_ip(download_test_env, monkeypatch):
    import httpx
    sessions, service, dl_dir = download_test_env

    with sessions() as db:
        v = Video(
            id="vid-ssrf-stream",
            platform="xiaohongshu",
            platform_video_id="66d3cb2b00000000200399dd",
            url="https://www.xiaohongshu.com/explore/66d3cb2b00000000200399dd",
            canonical_url="https://www.xiaohongshu.com/explore/66d3cb2b00000000200399dd",
            fingerprint="fp-ssrf",
            search_query="test",
            search_job_id="job1",
            is_mock=False,
        )
        db.add(v)
        db.flush()

        j = DownloadJob(id="job-ssrf-stream", video_id=v.id, platform="xiaohongshu", status="downloading")
        db.add(j)
        db.commit()

    requested_urls: list[str] = []

    class MockRedirectTransport(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
            requested_urls.append(str(request.url))
            if str(request.url) == "https://aweme.snssdk.com/video.mp4":
                # Redirect to private IP
                return httpx.Response(302, headers={"Location": "https://127.0.0.1/secret.mp4"})
            return httpx.Response(200, content=b"fake")

    monkeypatch.setattr(
        "backend.services.download.service.create_safe_async_client",
        lambda **kw: httpx.AsyncClient(transport=MockRedirectTransport(), follow_redirects=False),
    )

    resolved = ResolvedMedia(
        platform="xiaohongshu",
        video_id="66d3cb2b00000000200399dd",
        media_url="https://aweme.snssdk.com/video.mp4",
        adapter_name="fake",
        adapter_version="1.0",
    )

    await service._download_stream("job-ssrf-stream", resolved, v)

    # 1. Verify the private redirect destination was NEVER requested
    assert "https://127.0.0.1/secret.mp4" not in requested_urls
    assert requested_urls == ["https://aweme.snssdk.com/video.mp4"]

    # 2. Verify job failed with insecure_redirect
    with sessions() as db:
        job = db.get(DownloadJob, "job-ssrf-stream")
        assert job.status == "failed"
        assert job.error_code == "insecure_redirect"


@pytest.mark.anyio
async def test_resolve_xhs_short_link_blocks_redirect_to_private_ip(monkeypatch):
    import httpx

    from backend.services.identity_policy import resolve_xhs_short_link_async

    requested_urls: list[str] = []

    class MockShortLinkTransport(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
            requested_urls.append(str(request.url))
            if "xhslink.com" in str(request.url):
                # Redirect to private IP
                return httpx.Response(302, headers={"Location": "https://192.168.1.1/secret"})
            return httpx.Response(200)

    monkeypatch.setattr(
        "backend.services.identity_policy.create_safe_async_client",
        lambda **kw: httpx.AsyncClient(transport=MockShortLinkTransport(), follow_redirects=False),
    )

    result = await resolve_xhs_short_link_async("https://xhslink.com/fakeShortLink")
    assert result is None
    # Verify the private destination was never requested
    assert not any("192.168" in u for u in requested_urls)


@pytest.mark.anyio
async def test_dns_rebinding_blocked_at_transport(monkeypatch):
    import httpcore

    from backend.services.download.network import SafeAsyncNetworkBackend

    backend = SafeAsyncNetworkBackend()

    # Simulate DNS resolver returning a private IP (e.g. 127.0.0.1 or 192.168.1.1)
    async def mock_resolve(host, port=443):
        raise httpcore.ConnectError(f"Phân giải tên miền '{host}' trỏ tới địa chỉ IP bị cấm: 127.0.0.1.")

    monkeypatch.setattr("backend.services.download.network.resolve_and_validate_host_async", mock_resolve)

    with pytest.raises(httpcore.ConnectError, match="bị cấm"):
        await backend.connect_tcp("attacker-rebound.com", 443)


@pytest.mark.anyio
async def test_overall_download_job_timeout(download_test_env, monkeypatch):
    import asyncio
    sessions, service, dl_dir = download_test_env

    with sessions() as db:
        v = Video(
            id="vid-timeout-1",
            platform="xiaohongshu",
            platform_video_id="66d3cb2b00000000200399ee",
            url="https://www.xiaohongshu.com/explore/66d3cb2b00000000200399ee",
            canonical_url="https://www.xiaohongshu.com/explore/66d3cb2b00000000200399ee",
            fingerprint="fp-timeout",
            search_query="test",
            search_job_id="job1",
            is_mock=False,
        )
        db.add(v)
        db.flush()

        j = DownloadJob(id="job-timeout-1", video_id=v.id, platform="xiaohongshu", status="queued")
        db.add(j)
        db.commit()

    # Mock _download_stream to sleep longer than download_timeout_seconds
    async def mock_slow_stream(*args, **kwargs):
        await asyncio.sleep(10.0)

    async def mock_safe_url(url):
        return True

    monkeypatch.setattr("backend.services.download.service.is_safe_media_url_async", mock_safe_url)
    monkeypatch.setattr(service, "_download_stream", mock_slow_stream)
    monkeypatch.setattr("backend.services.download.service.settings.download_timeout_seconds", 0.1)

    await service._process_download_job("job-timeout-1")

    with sessions() as db:
        job = db.get(DownloadJob, "job-timeout-1")
        assert job.status == "failed"
        assert job.error_code == "download_timeout"


@pytest.mark.anyio
async def test_dns_cache_and_async_resolution():
    from backend.services.download.network import (
        _DNS_CACHE,
        resolve_and_validate_host_async,
        resolve_and_validate_host_sync,
    )

    # Resolve example.com
    ips_async = await resolve_and_validate_host_async("example.com", 443)
    assert len(ips_async) > 0
    assert ("example.com", 443) in _DNS_CACHE

    # Synchronous resolution uses cache
    ips_sync = resolve_and_validate_host_sync("example.com", 443)
    assert ips_sync == ips_async



