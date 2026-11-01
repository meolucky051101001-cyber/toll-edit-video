import os
import sys

import pytest

backend_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "backend")
if backend_dir not in sys.path:
    sys.path.insert(0, backend_dir)

import social_downloader
from social_downloader import (
    _extract_xhs_note_data,
    _is_xhs_blocked_url,
    _parse_xhs_initial_state,
    download_xiaohongshu,
)


def _state_html(note_body):
    return (
        "<script>window.__INITIAL_STATE__="
        '{"note":{"noteDetailMap":{"n1":{"note":' + note_body + "}}},"
        '"translate":{"originalCommentIds":new Set([]),"bannerVisible":undefined}}'
        "</script>"
    )


VIDEO_NOTE = (
    '{"title":"rednote video","type":"video","imageList":[{"url":"cover.jpg"}],'
    '"video":{"consumer":{},"media":{"stream":{"h264":['
    '{"streamType":258,"streamDesc":"X264_MP4","masterUrl":"https://cdn/258.mp4"}'
    "]}}}}"
)

ALBUM_NOTE = '{"title":"photo album","type":"normal","imageList":[{"url":"a.jpg"},{"url":"b.jpg"}]}'


def test_rednote_share_url_routes_to_xhs_scraper(monkeypatch, tmp_path):
    calls = {}

    def fake_scraper(url, output_dir, prefix):
        calls["url"] = url
        calls["dir"] = output_dir
        return True, "video.mp4", "title", ""

    monkeypatch.setattr(social_downloader, "download_xiaohongshu", fake_scraper)

    ok, path, title, err = social_downloader.download_social_video(
        "https://www.rednote.com/discovery/item/6adb3094000000002c016319?xsec_token=abc",
        str(tmp_path),
        "job",
    )

    assert ok is True
    assert path == "video.mp4"
    assert calls["url"].startswith("https://www.rednote.com/discovery/item/")


def test_rednote_state_parses_js_set_literals():
    state, raw = _parse_xhs_initial_state(_state_html(VIDEO_NOTE))

    assert state is not None
    assert "window.__INITIAL_STATE__" not in raw
    note = _extract_xhs_note_data(state)
    assert note["title"] == "rednote video"
    assert note["video"]["media"]["stream"]["h264"][0]["streamType"] == 258


@pytest.mark.parametrize(
    "url",
    [
        "https://www.rednote.com/login?redirectPath=http%3A",
        "https://www.rednote.com/404?source=/404/sec_abc",
        "https://www.rednote.com/404/sec_JRwwVlGS?redirectPath=http%3A",
        "https://www.rednote.com/",
        "https://www.rednote.com/explore",
        "https://www.xiaohongshu.com/explore",
        "https://www.xiaohongshu.com/login",
    ],
)
def test_xhs_login_and_404_redirects_are_blocked(url):
    assert _is_xhs_blocked_url(url) is True


def test_real_rednote_note_url_is_not_blocked():
    assert not _is_xhs_blocked_url(
        "https://www.rednote.com/discovery/item/6adb3094000000002c016319?xsec_token=abc"
    )


class _FakeResponse:
    status_code = 200

    def __init__(self, url, text):
        self.url = url
        self.text = text


def _run_scraper(monkeypatch, tmp_path, html):
    monkeypatch.setattr(
        social_downloader.requests,
        "get",
        lambda *args, **kwargs: _FakeResponse(
            "https://www.rednote.com/discovery/item/6adb3094000000002c016319", html
        ),
    )
    monkeypatch.setattr(social_downloader, "download_parallel_range", lambda *a, **k: False)

    def fake_stream(url, dest_path, **kwargs):
        with open(dest_path, "wb") as handle:
            handle.write(b"\0" * 2048)
        return True

    monkeypatch.setattr(social_downloader, "download_file_stream", fake_stream)
    return download_xiaohongshu(
        "https://www.rednote.com/discovery/item/6adb3094000000002c016319?xsec_token=abc",
        str(tmp_path),
        "job",
    )


def test_video_note_with_cover_image_is_not_treated_as_album(monkeypatch, tmp_path):
    ok, path, title, err = _run_scraper(monkeypatch, tmp_path, _state_html(VIDEO_NOTE))

    assert ok is True, err
    assert title == "rednote video"
    assert os.path.isfile(path)


def test_photo_album_is_rejected(monkeypatch, tmp_path):
    ok, path, title, err = _run_scraper(monkeypatch, tmp_path, _state_html(ALBUM_NOTE))

    assert ok is False
    assert "Album" in err


@pytest.mark.parametrize(
    "message",
    [
        "ERROR: Unsupported URL: https://www.rednote.com/login?redirectPath=http%3A",
        "Bài viết trên Tiểu Hồng Thư (XHS/Rednote) này không tồn tại, đã bị tác giả xóa, hết hạn hoặc yêu cầu đăng nhập",
        "Bài viết này là Album ảnh (không phải video)",
        "Video Tiểu Hồng Thư này chỉ có luồng dính logo watermark (Đã chặn tải để tránh video có logo)",
        "Không tìm thấy luồng video sạch trong bài viết Tiểu Hồng Thư",
    ],
)
def test_permanent_xhs_download_errors_are_not_retried(message):
    from v1_retry_policy import should_retry_job

    assert should_retry_job(RuntimeError(message)) is False


def test_transient_download_error_is_still_retried():
    from v1_retry_policy import should_retry_job

    assert should_retry_job(RuntimeError("Connection reset by peer")) is True
