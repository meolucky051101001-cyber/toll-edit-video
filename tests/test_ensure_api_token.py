from pathlib import Path

from scripts.ensure_api_token import ensure_api_token


def test_creates_env_with_token_when_missing(tmp_path: Path):
    target = tmp_path / ".env"
    token = ensure_api_token(target)
    assert len(token) >= 32
    assert f"API_TOKEN='{token}'" in target.read_text(encoding="utf-8") or f'API_TOKEN="{token}"' in target.read_text(encoding="utf-8")


def test_fills_empty_token_and_preserves_other_keys(tmp_path: Path):
    target = tmp_path / ".env"
    target.write_text("APP_ENV=development\nAPI_TOKEN=\nAI_MODEL=gemini-3.1-flash-lite\n", encoding="utf-8")
    token = ensure_api_token(target)
    content = target.read_text(encoding="utf-8")
    assert token in content
    assert "APP_ENV=development" in content
    assert "AI_MODEL=gemini-3.1-flash-lite" in content


def test_existing_token_untouched(tmp_path: Path):
    target = tmp_path / ".env"
    target.write_text("API_TOKEN=keep-me\n", encoding="utf-8")
    assert ensure_api_token(target) == "keep-me"
    assert target.read_text(encoding="utf-8") == "API_TOKEN=keep-me\n"


def test_no_staging_leftover(tmp_path: Path):
    target = tmp_path / ".env"
    ensure_api_token(target)
    assert [p.name for p in tmp_path.iterdir()] == [".env"]