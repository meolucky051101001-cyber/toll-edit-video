

from backend.providers.douyin.parser import (
    classify_page,
    parse_count_text,
    parse_detail,
    validate_image_url,
    video_url,
)


def test_douyin_video_url():
    # Valid Douyin URLs
    assert (
        video_url("https://www.douyin.com/video/7412345678901234567")
        == "https://www.douyin.com/video/7412345678901234567"
    )
    assert (
        video_url("https://www.douyin.com/video/7412345678901234567?extra=123")
        == "https://www.douyin.com/video/7412345678901234567"
    )
    assert (
        video_url("https://douyin.com/video/7123456789012345678/")
        == "https://www.douyin.com/video/7123456789012345678"
    )

    # Invalid URLs
    assert video_url("https://example.com/video/7412345678901234567") is None
    assert video_url("https://www.douyin.com/user/MS4wLjABAAAA") is None
    assert video_url("not_a_url") is None
    assert video_url("https://www.douyin.com/video/short") is None


def test_douyin_classify_page():
    # Verification / CAPTCHA
    assert classify_page("", title="验证码中间页") == "verification_required"
    assert (
        classify_page("请完成下列验证后继续 按住左边按钮拖动完成上方拼图")
        == "verification_required"
    )
    assert classify_page("拖动滑块完成拼图") == "verification_required"

    # Login
    assert classify_page("扫码登录查看更多精彩内容") == "login_required"
    assert (
        classify_page("", url="https://passport.douyin.com/page/login")
        == "login_required"
    )

    # Restricted
    assert classify_page("访问过于频繁，请稍后再试") == "restricted"
    assert classify_page("系统繁忙，请稍后再试") == "restricted"

    # Page available
    assert classify_page("发现精彩视频 搜索结果", title="文具开箱 - 抖音") == "page_available"


def test_douyin_parse_count_text():
    assert parse_count_text("1.5万") == 15000
    assert parse_count_text("2.3w") == 23000
    assert parse_count_text("10k") == 10000
    assert parse_count_text("350") == 350
    assert parse_count_text("0") == 0
    assert parse_count_text(None) is None
    assert parse_count_text("赞") is None
    assert parse_count_text("评论") is None


def test_douyin_validate_image_url():
    assert (
        validate_image_url("https://p3-pc.douyinpic.com/img/tos-cn-p-0015/test~c5_300x400.jpeg")
        is not None
    )
    assert (
        validate_image_url("https://p9-sign.byteimg.com/tos-cn-i-0813/test.jpeg?x-expires=123")
        is not None
    )
    assert (
        validate_image_url("//p3-pc.douyinpic.com/img/test.jpg")
        == "https://p3-pc.douyinpic.com/img/test.jpg"
    )
    assert validate_image_url("https://malicious.com/img.png") is None
    assert validate_image_url(None) is None


def test_douyin_parse_detail():
    raw_data = {
        "has_video": True,
        "title": "超好用文具开箱分享！",
        "description": "今天给大家带来一期超治愈的文具开箱 #文具 #开箱 #好物推荐",
        "poster": "https://p3-pc.douyinpic.com/img/cover.jpeg",
        "duration": 45.5,
        "author_name": "文具小达人",
        "author_url": "https://www.douyin.com/user/MS4wLjABAAAA_test",
        "like_text": "1.2万",
        "collect_text": "3450",
        "comment_text": "890",
        "share_text": "120",
    }
    url = "https://www.douyin.com/video/7412345678901234567"
    res = parse_detail(raw_data, url)

    assert res is not None
    assert res.platform == "douyin"
    assert res.platform_video_id == "7412345678901234567"
    assert str(res.url) == "https://www.douyin.com/video/7412345678901234567"
    assert res.title == "超好用文具开箱分享！"
    assert res.duration_seconds == 45.5
    assert res.author_name == "文具小达人"
    assert res.author_id == "MS4wLjABAAAA_test"
    assert res.like_count == 12000
    assert res.favorite_count == 3450
    assert res.comment_count == 890
    assert res.share_count == 120
    assert "文具" in res.hashtags
    assert "开箱" in res.hashtags


def test_douyin_search_api_validation(client):
    # Valid Douyin search
    resp = client.post(
        "/api/search",
        json={"query": "文具", "mode": "douyin", "platforms": ["douyin"], "limit": 20},
    )
    assert resp.status_code == 202
    data = resp.json()
    assert data["platforms"] == ["douyin"]

    # Limit > 100 rejected
    assert (
        client.post(
            "/api/search",
            json={"query": "文具", "mode": "douyin", "platforms": ["douyin"], "limit": 150},
        ).status_code
        == 422
    )

    # > 3 selected queries rejected in real mode
    assert (
        client.post(
            "/api/search",
            json={
                "query": "文具",
                "mode": "douyin",
                "platforms": ["douyin"],
                "selected_queries": ["a", "b", "c", "d"],
            },
        ).status_code
        == 422
    )


def test_douyin_browser_api(client):
    resp = client.get("/api/browser/douyin")
    assert resp.status_code == 200
    data = resp.json()
    assert "state" in data
    assert data["base_url"] == "https://www.douyin.com"
