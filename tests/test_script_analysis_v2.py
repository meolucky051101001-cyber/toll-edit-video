from fastapi.testclient import TestClient

from backend.app.main import app
from backend.core.database import SessionLocal
from backend.models.video import Video
from backend.schemas.script_analysis import ScriptAnalysisOut, StoryboardScene
from backend.services.ai_service import generate_fallback_script_analysis


def test_storyboard_scene_model():
    scene = StoryboardScene(
        scene_number=1,
        timestamp_range="0:00 - 0:03",
        visual_action="Cận cảnh sản phẩm",
        spoken_narration="Đây là sản phẩm tuyệt vời",
        on_screen_text="HOT 2026",
        sound_music="Nhạc nhẹ",
    )
    assert scene.scene_number == 1
    assert scene.timestamp_range == "0:00 - 0:03"
    dump = scene.model_dump()
    assert dump["on_screen_text"] == "HOT 2026"


def test_fallback_script_analysis_v2():
    video_data = {
        "title": "Unbox bàn phím cơ không dây mini",
        "caption": "Trải nghiệm gõ phím cực êm",
        "hashtags": ["banphimco", "setup"],
    }
    analysis = generate_fallback_script_analysis("v-fb-test", video_data)

    assert analysis.video_id == "v-fb-test"
    assert analysis.analysis_version == "v2.0"
    assert analysis.analysis_basis == "metadata"
    assert analysis.model_used == "fallback"
    assert len(analysis.storyboard) == 4

    s1 = analysis.storyboard[0]
    assert s1.scene_number == 1
    assert s1.timestamp_range == "0:00 - 0:03"
    assert "bàn phím cơ" in s1.spoken_narration.lower() or "bàn phím cơ" in s1.visual_action.lower()
    assert s1.on_screen_text != ""
    assert s1.sound_music != ""


def test_script_analysis_v1_backward_compatibility():
    legacy_v1_dict = {
        "video_id": "v-legacy",
        "hook_3s": "Hook 3s cu",
        "core_points": ["Diem 1", "Diem 2"],
        "retention_tactics": ["Chuyen canh nhanh"],
        "remake_script_vi": {
            "intro": "Mo dau",
            "body": ["Than bai 1", "Than bai 2"],
            "cta": "Follow kenh",
        },
        "source": "gemini",
        "cached": True,
    }
    # Must parse successfully without error even when storyboard & v2 metadata are missing
    parsed = ScriptAnalysisOut.model_validate(legacy_v1_dict)
    assert parsed.video_id == "v-legacy"
    assert parsed.storyboard == []
    assert parsed.analysis_version == "v2.0"
    assert parsed.analysis_basis == "metadata"


def test_api_analyze_script_v2_endpoint():
    with TestClient(app) as client:
        with SessionLocal() as db:
            vid = db.get(Video, "v-script-api-test")
            if not vid:
                vid = Video(
                    id="v-script-api-test",
                    platform="xiaohongshu",
                    platform_video_id="pscript1",
                    url="https://www.xiaohongshu.com/explore/pscript1",
                    canonical_url="https://www.xiaohongshu.com/explore/pscript1",
                    fingerprint="fp_script_1",
                    title="Review bình xịt chống thấm nano",
                    caption="Mẹo hay chống nước cho giày dép",
                    search_query="chống thấm nano",
                    search_job_id="job_script",
                    like_count=500,
                )
                db.add(vid)
                db.commit()

        # 1. First call creates analysis with storyboard
        res = client.post("/api/videos/v-script-api-test/script-analysis", json={"force_refresh": True})
        assert res.status_code == 200
        data = res.json()
        assert data["video_id"] == "v-script-api-test"
        assert "storyboard" in data
        assert len(data["storyboard"]) >= 3
        assert data["analysis_version"] == "v2.0"
        assert "analysis_basis" in data

        # 2. Second call returns cached analysis
        res_cached = client.post("/api/videos/v-script-api-test/script-analysis", json={"force_refresh": False})
        assert res_cached.status_code == 200
        data_cached = res_cached.json()
        assert data_cached["cached"] is True
