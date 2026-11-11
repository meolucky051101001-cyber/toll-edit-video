import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from backend.core.config import settings

TOKEN = "unit-test-token"


@pytest.fixture
def token(monkeypatch):
    monkeypatch.setattr(settings, "api_token", SecretStr(TOKEN))
    return TOKEN


def auth(token: str) -> dict[str, str]:
    return {"X-API-Token": token}


def test_empty_token_keeps_auth_off(client: TestClient):
    assert client.get("/api/settings").status_code == 200


def test_missing_token_rejected(client: TestClient, token):
    assert client.get("/api/settings").status_code == 401


def test_wrong_token_rejected(client: TestClient, token):
    assert client.get("/api/settings", headers=auth("not-the-token")).status_code == 401


def test_valid_token_accepted(client: TestClient, token):
    assert client.get("/api/settings", headers=auth(token)).status_code == 200


def test_health_stays_exempt(client: TestClient, token):
    assert client.get("/api/health").status_code == 200


def test_screenshot_requires_token(client: TestClient, token):
    assert client.get("/api/browser/xiaohongshu/screenshot").status_code == 401
    assert client.get("/api/browser/douyin/screenshot").status_code == 401


def test_screenshot_with_token_passes_auth(client: TestClient, token):
    res = client.get("/api/browser/xiaohongshu/screenshot", headers=auth(token))
    assert res.status_code != 401


def test_events_require_token(client: TestClient, token):
    assert client.get("/api/events").status_code == 401


def test_system_metrics_require_token(client: TestClient, token):
    assert client.get("/api/system/metrics").status_code == 401
    assert client.get("/api/system/metrics", headers=auth(token)).status_code == 200