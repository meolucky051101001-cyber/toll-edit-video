"""DeepSeek fallback can target an OpenAI-compatible endpoint and model list."""
import json
from types import SimpleNamespace

from backend.ai import translation


def capture(monkeypatch, reply):
    calls = []

    def post(url, json=None, headers=None, timeout=None):
        calls.append((url, json["model"], headers["Authorization"]))
        return SimpleNamespace(status_code=200, json=lambda: {"choices": [{"message": {"content": reply}}]})

    monkeypatch.setattr(translation.requests, "post", post)
    return calls


def test_configured_endpoint_and_models_are_used(monkeypatch):
    monkeypatch.setenv("DEEPSEEK_BASE_URL", "https://proxy.example/v1/")
    monkeypatch.setenv("DEEPSEEK_MODELS", "custom-flash-free")
    calls = capture(monkeypatch, json.dumps(["Xin chào", "Tạm biệt"]))
    result = translation.translate_with_deepseek(["你好", "再见"], "vi", api_key="hms-test")
    assert result == ["Xin chào", "Tạm biệt"]
    assert calls == [("https://proxy.example/v1/chat/completions", "custom-flash-free", "Bearer hms-test")]


def test_defaults_keep_official_endpoint(monkeypatch):
    monkeypatch.delenv("DEEPSEEK_BASE_URL", raising=False)
    monkeypatch.delenv("DEEPSEEK_MODELS", raising=False)
    calls = capture(monkeypatch, json.dumps(["Xin chào"]))
    assert translation.translate_with_deepseek(["你好"], "vi", api_key="sk-test") == ["Xin chào"]
    assert calls[0][0] == "https://api.deepseek.com/v1/chat/completions"
    assert calls[0][1] == "deepseek-v4"
