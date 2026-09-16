import pytest

from backend.schemas.contracts import VideoResult
from backend.services.media_parser import extract_hashtags, parse_duration_seconds


@pytest.mark.parametrize(
    "input_val,expected",
    [
        ("00:45", 45),
        ("02:15", 135),
        ("01:10:05", 4205),
        ("1:05", 65),
        ("45秒", 45),
        ("1分30秒", 90),
        ("2小时15分", 8100),
        ("1h 30m", 5400),
        ("45s", 45),
        (55, 55),
        (82.4, 82),
        (82.7, 83),
        (None, None),
        ("", None),
        ("   ", None),
        ("invalid", None),
    ],
)
def test_parse_duration_seconds(input_val, expected):
    assert parse_duration_seconds(input_val) == expected


def test_extract_hashtags():
    text = "Học cách làm bánh bao ngon tại nhà #cachlambanhbao #amthuctrunghoa #banhbao! #cachlambanhbao"
    tags = extract_hashtags(text)
    assert "cachlambanhbao" in tags
    assert "amthuctrunghoa" in tags
    assert "banhbao" in tags
    # Deduplication
    assert len(tags) == 3


def test_extract_chinese_hashtags():
    text = "手把手教你在家自制包子 #包子教程 #家常美食 #早餐灵感！"
    tags = extract_hashtags(text)
    assert tags == ["包子教程", "家常美食", "早餐灵感"]


def test_extract_hashtags_empty():
    assert extract_hashtags(None) == []
    assert extract_hashtags("") == []
    assert extract_hashtags("Không có hashtag nào ở đây.") == []


def test_video_result_accepts_duration_and_hashtags():
    vr = VideoResult(
        platform="douyin",
        platform_video_id="71234567890",
        url="https://www.douyin.com/video/71234567890",
        title="Bánh bao nhân thịt #banhbao",
        duration_seconds=125,
        hashtags=["banhbao"],
        is_mock=False,
    )
    assert vr.duration_seconds == 125
    assert vr.hashtags == ["banhbao"]
