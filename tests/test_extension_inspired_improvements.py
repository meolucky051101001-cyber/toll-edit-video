import asyncio
import httpx
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend.core.database import Base
from backend.models.video import Video
from backend.models.download_job import DownloadJob
from backend.schemas.downloads import ResolvedMedia
from backend.services.download.base import BaseDownloadAdapter, DownloadAdapterError
from backend.services.download.douyin_adapter import DouyinDownloadAdapter
from backend.services.download.xhs_adapter import XhsDownloadAdapter
from backend.services.download.browser_resolver import BrowserMediaResolver
from backend.services.download.service import DownloadService


def test_douyin_adapter_prefers_higher_resolution_over_higher_bitrate(monkeypatch):
    """Verifies that Douyin adapter prioritizes pixel area (width*height) over raw bitrate value."""
    adapter = DouyinDownloadAdapter("http://127.0.0.1:5555")
    video = Video(
        id="vid-dy-1",
        platform="douyin",
        platform_video_id="7352345678901234567",
        url="https://www.douyin.com/video/7352345678901234567",
        canonical_url="https://www.douyin.com/video/7352345678901234567",
        fingerprint="fp-dy",
    )

    # Stream 1: 720p with 5000kbps bitrate
    # Stream 2: 1080p with 3000kbps bitrate (higher resolution!)
    mock_resp = {
        "data": {
            "video_id": "7352345678901234567",
            "type": "video",
            "aweme_detail": {
                "aweme_id": "7352345678901234567",
                "video": {
                    "bit_rate": [
                        {
                            "bit_rate": 5000000,
                            "play_addr": {
                                "width": 1280,
                                "height": 720,
                                "url_list": ["https://aweme.snssdk.com/playwm/?video_id=720p_stream"],
                            },
                        },
                        {
                            "bit_rate": 3000000,
                            "play_addr": {
                                "width": 1920,
                                "height": 1080,
                                "url_list": ["https://aweme.snssdk.com/playwm/?video_id=1080p_stream"],
                            },
                        },
                    ]
                },
            },
        }
    }

    async def mock_get(self, url, **kwargs):
        return httpx.Response(200, json=mock_resp)

    monkeypatch.setattr(httpx.AsyncClient, "get", mock_get)

    resolved = asyncio.run(adapter.resolve(video))
    assert resolved.platform == "douyin"
    assert resolved.video_id == "7352345678901234567"
    # Should pick 1080p stream and strip watermark playwm -> play
    assert resolved.media_url == "https://aweme.snssdk.com/play/?video_id=1080p_stream"


def test_douyin_adapter_falls_back_to_embedded_images_video(monkeypatch):
    """Verifies that Douyin adapter extracts video from dynamic/live photo post if main video is empty."""
    adapter = DouyinDownloadAdapter("http://127.0.0.1:5555")
    video = Video(
        id="vid-dy-img",
        platform="douyin",
        platform_video_id="7352345678901234568",
        url="https://www.douyin.com/video/7352345678901234568",
        canonical_url="https://www.douyin.com/video/7352345678901234568",
        fingerprint="fp-dy2",
    )

    mock_resp = {
        "data": {
            "video_id": "7352345678901234568",
            "type": "video",
            "aweme_detail": {
                "aweme_id": "7352345678901234568",
                "video": {},
                "images": [
                    {
                        "video": {
                            "bit_rate": [
                                {
                                    "bit_rate": 2000000,
                                    "play_addr": {
                                        "width": 1080,
                                        "height": 1920,
                                        "url_list": ["https://aweme.snssdk.com/playwm/?video_id=embedded_img_video"],
                                    },
                                }
                            ]
                        }
                    }
                ],
            },
        }
    }

    async def mock_get(self, url, **kwargs):
        return httpx.Response(200, json=mock_resp)

    monkeypatch.setattr(httpx.AsyncClient, "get", mock_get)

    resolved = asyncio.run(adapter.resolve(video))
    assert resolved.media_url == "https://aweme.snssdk.com/play/?video_id=embedded_img_video"


def test_xhs_adapter_extracts_live_photo_video_stream(monkeypatch):
    """Verifies that XHS adapter extracts video stream from Live Photo notes."""
    adapter = XhsDownloadAdapter("http://127.0.0.1:5556")
    nid = "66d3cb2b00000000200399e3"
    video = Video(
        id="vid-xhs-live",
        platform="xiaohongshu",
        platform_video_id=nid,
        url=f"https://www.xiaohongshu.com/explore/{nid}",
        canonical_url=f"https://www.xiaohongshu.com/explore/{nid}",
        fingerprint="fp-xhs",
    )

    # A Live Photo post (type: 图文, but with live_photo and stream codecs)
    mock_resp = {
        "data": {
            "作品ID": nid,
            "作品类型": "图文",
            "live_photo": True,
            "stream": {
                "EF4": [
                    {
                        "width": 1080,
                        "height": 1440,
                        "avg_bitrate": 4000,
                        "master_url": "https://sns-video-bd.xhscdn.com/stream/ef4_1080.mp4",
                    }
                ],
                "EF6": [
                    {
                        "width": 720,
                        "height": 960,
                        "avg_bitrate": 2000,
                        "master_url": "https://sns-video-bd.xhscdn.com/stream/ef6_720.mp4",
                    }
                ],
            },
        }
    }

    async def mock_post(self, url, **kwargs):
        return httpx.Response(200, json=mock_resp)

    monkeypatch.setattr(httpx.AsyncClient, "post", mock_post)

    resolved = asyncio.run(adapter.resolve(video))
    assert resolved.platform == "xiaohongshu"
    assert resolved.video_id == nid
    assert resolved.media_url == "https://sns-video-bd.xhscdn.com/stream/ef4_1080.mp4"


def test_xhs_adapter_prefers_origin_video_key_over_live_streams(monkeypatch):
    """Verifies that originVideoKey remains top priority for standard video posts."""
    adapter = XhsDownloadAdapter("http://127.0.0.1:5556")
    nid = "66d3cb2b00000000200399e3"
    video = Video(
        id="vid-xhs-origin",
        platform="xiaohongshu",
        platform_video_id=nid,
        url=f"https://www.xiaohongshu.com/explore/{nid}",
        canonical_url=f"https://www.xiaohongshu.com/explore/{nid}",
        fingerprint="fp-xhs2",
    )

    mock_resp = {
        "data": {
            "作品ID": nid,
            "作品类型": "视频",
            "originVideoKey": "ORIGIN_KEY_12345",
            "下载地址": ["https://sns-video-bd.xhscdn.com/stream/compressed.mp4"],
        }
    }

    async def mock_post(self, url, **kwargs):
        return httpx.Response(200, json=mock_resp)

    monkeypatch.setattr(httpx.AsyncClient, "post", mock_post)

    resolved = asyncio.run(adapter.resolve(video))
    assert resolved.media_url == "https://sns-video-bd.xhscdn.com/ORIGIN_KEY_12345"


@pytest.mark.anyio
async def test_browser_fallback_resolver_triggered_on_adapter_failure(monkeypatch, tmp_path):
    """Verifies that when primary adapter fails with service_unavailable, BrowserMediaResolver is used as fallback."""
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine, expire_on_commit=False)

    dl_dir = tmp_path / "downloads"
    dl_dir.mkdir()

    class FailingAdapter(BaseDownloadAdapter):
        @property
        def name(self) -> str:
            return "FailingAdapter"

        @property
        def version(self) -> str:
            return "failing:1.0"

        async def health_check(self) -> bool:
            return False

        async def resolve(self, video: Video) -> ResolvedMedia:
            raise DownloadAdapterError("service_unavailable", "Primary service offline")

    class MockBrowserResolver:
        async def resolve(self, video: Video) -> ResolvedMedia:
            return ResolvedMedia(
                platform=video.platform,
                video_id=video.platform_video_id or "vid",
                media_url="https://sns-video-bd.xhscdn.com/fallback_success.mp4",
                headers={"Referer": "https://www.xiaohongshu.com/"},
                format="mp4",
                adapter_name="BrowserInPageResolver",
                adapter_version="browser:in-page-v1",
            )

    service = DownloadService(
        sessions,
        xhs_adapter=FailingAdapter(),
        download_dir=dl_dir,
        browser_resolver=MockBrowserResolver(),
    )
    service.temp_dir = dl_dir / ".temp"
    service.temp_dir.mkdir(exist_ok=True)

    with sessions() as db:
        video = Video(
            id="vid-fb-test",
            platform="xiaohongshu",
            platform_video_id="66d3cb2b00000000200399e3",
            url="https://www.xiaohongshu.com/explore/66d3cb2b00000000200399e3",
            canonical_url="https://www.xiaohongshu.com/explore/66d3cb2b00000000200399e3",
            fingerprint="fp-fb",
            search_query="test",
            search_job_id="job1",
        )
        db.add(video)
        db.commit()

    # Intercept streaming to verify resolved object without actually downloading
    resolved_holder = []

    async def mock_download_stream(download_id, resolved, target_video):
        resolved_holder.append(resolved)

    async def mock_safe(u):
        return True

    monkeypatch.setattr(service, "_download_stream", mock_download_stream)
    monkeypatch.setattr("backend.services.download.service.is_safe_media_url_async", mock_safe)

    # Request download
    job_out = service.request_download("vid-fb-test")
    # Await background task or process directly
    task = service.active_tasks.get(job_out.id)
    if task:
        await task
    else:
        await service._process_download_job(job_out.id)

    assert len(resolved_holder) == 1
    assert resolved_holder[0].adapter_name == "BrowserInPageResolver"
    assert resolved_holder[0].media_url == "https://sns-video-bd.xhscdn.com/fallback_success.mp4"


@pytest.mark.anyio
async def test_download_stream_preserves_platform_referer_on_redirect(monkeypatch, tmp_path):
    """Verifies that when redirecting across hosts, credentials are removed but platform Referer is preserved."""
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine, expire_on_commit=False)

    dl_dir = tmp_path / "downloads"
    dl_dir.mkdir()

    service = DownloadService(
        sessions,
        download_dir=dl_dir,
    )
    service.temp_dir = dl_dir / ".temp"
    service.temp_dir.mkdir(exist_ok=True)

    with sessions() as db:
        video = Video(
            id="vid-ref-test",
            platform="douyin",
            platform_video_id="7352345678901234567",
            url="https://www.douyin.com/video/7352345678901234567",
            canonical_url="https://www.douyin.com/video/7352345678901234567",
            fingerprint="fp-ref",
            search_query="test",
            search_job_id="job1",
        )
        db.add(video)
        db.commit()

    captured_requests = []

    class MockStreamClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc_val, exc_tb):
            pass

        def build_request(self, method, url, headers=None):
            req = httpx.Request(method, url, headers=headers)
            captured_requests.append((url, dict(headers or {})))
            return req

        async def send(self, req, stream=False):
            url_str = str(req.url)
            if "snssdk.com" in url_str:
                # 302 redirect to douyinvod CDN host
                resp = httpx.Response(
                    302,
                    headers={"Location": "https://v3-dy-o.douyinvod.com/video_stream.mp4"},
                    request=req,
                )
                return resp
            else:
                # 200 OK stream response on CDN
                async def mock_aiter(chunk_size=65536):
                    yield b"fake_mp4_bytes_for_testing"

                resp = httpx.Response(
                    200,
                    headers={"Content-Length": "26", "Content-Type": "video/mp4"},
                    request=req,
                )
                resp.aiter_bytes = mock_aiter
                return resp

    monkeypatch.setattr("backend.services.download.service.create_safe_async_client", lambda **kw: MockStreamClient())
    monkeypatch.setattr("backend.services.download.service.is_safe_media_url_async", lambda u: asyncio.sleep(0, result=True))

    resolved = ResolvedMedia(
        platform="douyin",
        video_id="7352345678901234567",
        media_url="https://aweme.snssdk.com/video_redirect",
        headers={
            "User-Agent": "CustomUA",
            "Cookie": "secret_cookie=123",
            "Authorization": "Bearer secret_token",
            "Referer": "https://www.douyin.com/",
        },
        format="mp4",
        adapter_name="DouyinAdapter",
        adapter_version="1.0",
    )

    with sessions() as db:
        job = DownloadJob(
            id="job-ref-test",
            video_id="vid-ref-test",
            platform="douyin",
            status="downloading",
        )
        db.add(job)
        db.commit()

    await service._download_stream("job-ref-test", resolved, video)

    # We should have 2 requests: initial snssdk.com and redirected douyinvod.com
    assert len(captured_requests) == 2

    first_url, first_headers = captured_requests[0]
    first_h = httpx.Headers(first_headers)
    assert "snssdk.com" in first_url
    assert first_h.get("cookie") == "secret_cookie=123"
    assert first_h.get("referer") == "https://www.douyin.com/"

    second_url, second_headers = captured_requests[1]
    second_h = httpx.Headers(second_headers)
    assert "douyinvod.com" in second_url
    # Cookie and Authorization stripped across hosts for security
    assert "cookie" not in second_h
    assert "authorization" not in second_h
    # Platform Referer maintained on CDN to avoid 403 Forbidden
    assert second_h.get("referer") == "https://www.douyin.com/"
    assert second_h.get("user-agent") == "CustomUA"



