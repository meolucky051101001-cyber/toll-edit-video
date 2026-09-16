import asyncio

from backend.app.main import app


def test_health_validation_and_origin(client):
    assert client.get("/api/health").json()["status"] == "online"
    assert client.post("/api/search", json={"query": " "}).status_code == 422
    assert client.get("/api/videos?sort=bad").status_code == 422
    assert (
        client.post(
            "/api/search", json={"query": "hi"}, headers={"origin": "https://evil.invalid"}
        ).status_code
        == 403
    )
    assert client.get("/api/videos/no-such-id").status_code == 404


def test_filters_pagination_and_save(client, sessions):
    from tests.test_pipeline import make_job

    job_id = make_job(sessions, 50)
    asyncio.run(app.state.search.run(job_id))
    result = client.get(f"/api/videos?job_id={job_id}&page_size=10&sort=likes").json()
    assert result["total"] == 50 and len(result["items"]) == 10
    likes = [v["like_count"] for v in result["items"]]
    assert likes == sorted(likes, reverse=True)
    video_id = result["items"][0]["id"]
    assert client.post(f"/api/videos/{video_id}/save").json()["status"] == "saved"
    saved = client.get("/api/videos?status=saved").json()
    assert saved["total"] == 1 and saved["items"][0]["id"] == video_id
    assert client.post(f"/api/videos/{video_id}/skip").json()["status"] == "skipped"
    assert client.get("/api/videos?status=saved").json()["total"] == 0
    assert client.get("/api/videos?platform=douyin").json()["total"] == 25
    assert client.get("/api/videos?q=%25").json()["total"] == 0
    assert client.get("/api/videos?min_relevance=80").json()["total"] > 0
    assert client.get("/api/videos?since=2099-01-01T00:00:00").json()["total"] == 0


def test_settings_write_only_secret_and_preserve_key(client, tmp_path, monkeypatch):
    from backend.api import settings as settings_api
    from backend.services.ai_service import AIService
    from tests.test_ai import config

    cfg = config()
    monkeypatch.setattr(settings_api, "ROOT", tmp_path)
    monkeypatch.setattr(settings_api, "settings", cfg)
    app.state.ai = AIService(cfg)
    (tmp_path / ".env").write_text("UNCHANGED=yes\nAI_API_KEY='test-secret'\n", encoding="utf-8")
    body = {
        "ai_provider": "gemini",
        "ai_model": "gemini-3.1-flash-lite",
        "ai_free_tier_confirmed": True,
    }
    response = client.patch("/api/settings", json=body)
    assert response.status_code == 200 and response.json()["ai_key_configured"]
    assert "test-secret" not in response.text
    assert "test-secret" in (tmp_path / ".env").read_text(encoding="utf-8")
    body["ai_api_key"] = "replacement-secret"
    assert client.patch("/api/settings", json=body).status_code == 200
    assert cfg.ai_api_key.get_secret_value() == "replacement-secret"
    assert "replacement-secret" not in client.get("/api/settings").text
    assert "UNCHANGED=yes" in (tmp_path / ".env").read_text(encoding="utf-8")
    assert not list(tmp_path.glob(".env.*"))
    body["ai_model"] = "paid-model"
    rejected = client.patch("/api/settings", json=body)
    assert rejected.status_code == 422 and "replacement-secret" not in rejected.text


def test_expansion_endpoint_and_old_job_compatibility(client, sessions):
    import json

    from backend.services.ai_service import AIService
    from tests.test_ai import FakeAI, config, payload
    from tests.test_pipeline import make_job

    fake = FakeAI([json.dumps(payload())])
    app.state.ai = AIService(config(), lambda _: fake)
    response = client.post("/api/search/expand", json={"query": "unbox đồ cute"})
    assert response.status_code == 200 and response.json()["source"] == "gemini"
    job_id = make_job(sessions)
    job = client.get(f"/api/search/jobs/{job_id}").json()
    assert job["queries"] == ["unbox đồ cute"] and job["ai_source"] == "original"
