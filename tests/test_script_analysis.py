import json
import pytest
from unittest.mock import AsyncMock, MagicMock

from backend.models.video import Video
from backend.schemas.script_analysis import ScriptAnalysisOut, RemakeScriptVI
from backend.services.ai_service import AIService, generate_fallback_script_analysis
from backend.providers.ai.base import AIProvider, AIError
from backend.core.config import Settings
from pydantic import SecretStr


def make_video(vid_id, platform, pvid, url, title, caption="", hashtags=None):
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
        caption=caption,
        hashtags=hashtags or [],
        duration_seconds=45,
    )


def test_generate_fallback_script_analysis():
    video_data = {
        "title": "Mẹo unbox đồ chơi bất ngờ",
        "caption": "Trải nghiệm mở hộp cực kỳ thú vị và nhiều bất ngờ",
        "hashtags": ["unbox", "toys", "review"],
        "platform": "douyin",
    }
    result = generate_fallback_script_analysis("vid-test-1", video_data)
    assert isinstance(result, ScriptAnalysisOut)
    assert result.video_id == "vid-test-1"
    assert result.source == "fallback"
    assert len(result.hook_3s) > 10
    assert len(result.core_points) >= 3
    assert len(result.retention_tactics) >= 3
    assert isinstance(result.remake_script_vi, RemakeScriptVI)
    assert len(result.remake_script_vi.intro) > 5
    assert len(result.remake_script_vi.body) >= 3
    assert len(result.remake_script_vi.cta) > 5


@pytest.mark.anyio
async def test_ai_service_analyze_script_with_mock_provider():
    config = Settings(
        _env_file=None,
        ai_provider="gemini",
        ai_api_key=SecretStr("valid-key"),
        ai_model="gemini-3.1-flash-lite",
        ai_free_tier_confirmed=True,
        ai_min_interval=0,
    )

    mock_response = {
        "video_id": "vid-1",
        "hook_3s": "Hook cực cuốn về bóc hộp",
        "core_points": ["Điểm 1", "Điểm 2", "Điểm 3"],
        "retention_tactics": ["Chuyển cảnh nhanh", "Nhạc dồn dập"],
        "remake_script_vi": {
            "intro": "Mở đầu hấp dẫn",
            "body": ["Thân bài 1", "Thân bài 2"],
            "cta": "Bấm lưu video ngay",
        },
        "source": "gemini",
        "cached": False,
    }

    mock_provider = MagicMock(spec=AIProvider)
    mock_provider.analyze_script = AsyncMock(return_value=json.dumps(mock_response))

    service = AIService(config=config, factory=lambda cfg: mock_provider)
    res = await service.analyze_script("vid-1", {"title": "Test Video"})

    assert res.source == "gemini"
    assert res.hook_3s == "Hook cực cuốn về bóc hộp"
    assert len(res.core_points) == 3
    assert res.remake_script_vi.intro == "Mở đầu hấp dẫn"


@pytest.mark.anyio
async def test_ai_service_analyze_script_fallback_on_error():
    config = Settings(
        _env_file=None,
        ai_provider="gemini",
        ai_api_key=SecretStr("valid-key"),
        ai_model="gemini-3.1-flash-lite",
        ai_free_tier_confirmed=True,
        ai_min_interval=0,
    )

    mock_provider = MagicMock(spec=AIProvider)
    mock_provider.analyze_script = AsyncMock(side_effect=AIError("network", "Connection failed"))

    service = AIService(config=config, factory=lambda cfg: mock_provider)
    res = await service.analyze_script("vid-1", {"title": "Test Fallback Video"})

    assert res.source == "fallback"
    assert res.video_id == "vid-1"
    assert len(res.hook_3s) > 0


def test_api_video_script_analysis_flow(client, sessions):
    with sessions() as db:
        v = make_video(
            "vid-analysis-1",
            "douyin",
            "dy_analysis_101",
            "https://www.douyin.com/video/1000000000000000099",
            "Hướng dẫn nấu ăn siêu ngon",
            caption="Bí quyết ướp thịt nướng mềm thơm chuẩn vị nhà hàng",
            hashtags=["nauan", "food", "monngon"],
        )
        db.add(v)
        db.commit()

    # 1. 404 for non-existent video
    resp_404 = client.post("/api/videos/non-existent-id/script-analysis", json={})
    assert resp_404.status_code == 404

    # 2. First analysis call: should generate and save to DB
    resp = client.post("/api/videos/vid-analysis-1/script-analysis", json={})
    assert resp.status_code == 200
    data = resp.json()
    assert data["video_id"] == "vid-analysis-1"
    assert data["cached"] is False
    assert "hook_3s" in data
    assert "remake_script_vi" in data
    assert "intro" in data["remake_script_vi"]
    assert "body" in data["remake_script_vi"]
    assert "cta" in data["remake_script_vi"]

    # 3. Second call without force_refresh: should return cached=True
    resp_cached = client.post("/api/videos/vid-analysis-1/script-analysis", json={"force_refresh": False})
    assert resp_cached.status_code == 200
    data_cached = resp_cached.json()
    assert data_cached["cached"] is True
    assert data_cached["hook_3s"] == data["hook_3s"]

    # 4. Third call with force_refresh: should re-analyze
    resp_forced = client.post("/api/videos/vid-analysis-1/script-analysis", json={"force_refresh": True})
    assert resp_forced.status_code == 200
    data_forced = resp_forced.json()
    assert data_forced["cached"] is False
