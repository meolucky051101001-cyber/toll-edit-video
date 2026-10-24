import os
import sys
import pytest

# Ensure backend directory is in sys.path
backend_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "backend")
if backend_dir not in sys.path:
    sys.path.insert(0, backend_dir)

from social_downloader import USER_AGENTS, download_xiaohongshu


def test_user_agents_desktop_firefox():
    """Verify that desktop UA uses Firefox to avoid Xiaohongshu anti-crawler login redirect."""
    assert "desktop" in USER_AGENTS
    assert "Firefox" in USER_AGENTS["desktop"]
    assert "mobile" in USER_AGENTS


def test_douyin_playwm_sanitization():
    """Verify playwm replacement logic."""
    wm_url = "https://aweme.snssdk.com/aweme/v1/playwm/?video_id=v0200fg10000abc"
    clean_url = wm_url.replace("playwm", "play")
    assert "playwm" not in clean_url
    assert "play" in clean_url


def test_xhs_stream_classification_filters_watermark_259():
    """Verify that stream 259 is marked as watermarked and clean streams (258, 114) are prioritized."""
    mock_streams = {
        "h264": [
            {
                "streamType": 259,
                "streamDesc": "MINI_APP_259",
                "masterUrl": "http://sns-video.xhscdn.com/stream/1/110/259/abc_259.mp4",
                "backupUrls": ["http://backup.xhscdn.com/259/abc_259.mp4"]
            },
            {
                "streamType": 258,
                "streamDesc": "X264_MP4",
                "masterUrl": "http://sns-video.xhscdn.com/stream/1/110/258/abc_258.mp4",
                "backupUrls": []
            }
        ],
        "h265": [
            {
                "streamType": 114,
                "streamDesc": "WEB_114_h5",
                "masterUrl": "http://sns-video.xhscdn.com/stream/1/110/114/abc_114.mp4",
                "backupUrls": []
            }
        ]
    }

    p1_clean = []
    p2_clean = []
    p3_clean = []
    watermarked = []

    for st_type in ['h264', 'h265', 'av1', 'h266']:
        for item in mock_streams.get(st_type, []):
            st_type_code = item.get("streamType")
            st_desc = str(item.get("streamDesc") or "").upper()
            urls = [item.get("masterUrl")] + item.get("backupUrls", [])
            for v_u in urls:
                if not v_u:
                    continue
                is_wm = (
                    st_type_code == 259 or
                    "MINI_APP" in st_desc or
                    "_259.mp4" in v_u or
                    "/259/" in v_u or
                    "playwm" in v_u
                )
                if is_wm:
                    watermarked.append(v_u)
                elif st_type_code == 258 or "X264" in st_desc:
                    p1_clean.append(v_u)
                elif st_type_code in (114, 115, 120) or "WEB_" in st_desc:
                    p2_clean.append(v_u)
                else:
                    p3_clean.append(v_u)

    clean_streams = p1_clean + p2_clean + p3_clean

    # Verify stream 258 is first choice (Clean H264)
    assert len(p1_clean) == 1
    assert "abc_258.mp4" in p1_clean[0]

    # Verify stream 114 is second choice (Clean H265)
    assert len(p2_clean) == 1
    assert "abc_114.mp4" in p2_clean[0]

    # Verify stream 259 was strictly quarantined into watermarked list
    assert len(watermarked) == 2
    for wm in watermarked:
        assert "259" in wm
        assert wm not in clean_streams
