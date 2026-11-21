import ast
import asyncio
import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from backend import telegram_access, v1_preflight


def test_parse_chat_ids_extracts_every_integer_token():
    assert telegram_access.parse_chat_ids("") == frozenset()
    assert telegram_access.parse_chat_ids("1, 2;3 x -4") == frozenset({1, 2, 3, -4})


def test_load_allowed_chat_ids_prefers_environment_then_reads_env_file(monkeypatch, tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text("TELEGRAM_ALLOWED_CHAT_IDS=21, 22\n", encoding="utf-8")
    monkeypatch.delenv("TELEGRAM_ALLOWED_CHAT_IDS", raising=False)
    assert telegram_access.load_allowed_chat_ids(env_file=env_file) == frozenset({21, 22})

    monkeypatch.setenv("TELEGRAM_ALLOWED_CHAT_IDS", "31;32")
    assert telegram_access.load_allowed_chat_ids(env_file=env_file) == frozenset({31, 32})

    assert telegram_access.load_allowed_chat_ids(env_file=tmp_path / "missing.env") == frozenset({31, 32})
    monkeypatch.delenv("TELEGRAM_ALLOWED_CHAT_IDS")
    assert telegram_access.load_allowed_chat_ids(env_file=tmp_path / "missing.env") == frozenset()


def test_is_update_allowed_accepts_chat_or_user_ids_and_open_empty_list():
    update = SimpleNamespace(
        effective_chat=SimpleNamespace(id=101),
        effective_user=SimpleNamespace(id=202),
    )
    assert telegram_access.is_update_allowed(update, frozenset())
    assert telegram_access.is_update_allowed(update, frozenset({101}))
    assert telegram_access.is_update_allowed(update, frozenset({202}))
    assert not telegram_access.is_update_allowed(update, frozenset({303}))

    missing = SimpleNamespace(effective_chat=None, effective_user=None)
    assert not telegram_access.is_update_allowed(missing, frozenset({101}))


def test_install_access_gate_with_empty_list_warns_and_adds_no_handler(caplog):
    class FakeApplication:
        def __init__(self):
            self.handlers = []

        def add_handler(self, handler, group=0):
            self.handlers.append((handler, group))

    application = FakeApplication()
    assert not telegram_access.install_access_gate(application, frozenset(), bot_name="test")
    assert application.handlers == []
    assert "open to everyone" in caplog.text
    assert "TELEGRAM_ALLOWED_CHAT_IDS" in caplog.text


def test_access_gate_blocks_updates_when_telegram_package_is_available():
    pytest.importorskip("telegram")
    from telegram.ext import ApplicationHandlerStop

    class FakeApplication:
        def __init__(self):
            self.handlers = []

        def add_handler(self, handler, group=0):
            self.handlers.append((handler, group))

    application = FakeApplication()
    assert telegram_access.install_access_gate(application, frozenset({101}), bot_name="test")
    handler, group = application.handlers[0]
    assert group == -2000
    message = SimpleNamespace(reply_text=AsyncMock())
    update = SimpleNamespace(
        effective_chat=SimpleNamespace(id=303),
        effective_user=SimpleNamespace(id=404),
        effective_message=message,
    )
    with pytest.raises(ApplicationHandlerStop):
        asyncio.run(handler.callback(update, None))
    message.reply_text.assert_awaited_once_with(
        "⛔ Bot này chỉ dành cho chủ máy. Chat ID của bạn: 303"
    )


def test_both_bot_entrypoints_install_the_access_gate():
    root = Path(__file__).resolve().parents[1]
    for source_path in (root / "backend" / "telegram_bot.py", root / "telebot_video_downloader.py"):
        tree = ast.parse(source_path.read_text(encoding="utf-8"))
        assert any(
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "install_access_gate"
            for node in ast.walk(tree)
        ), source_path


def test_preflight_reports_telegram_allowlist_configuration(monkeypatch, tmp_path):
    def configure_preflight(env):
        monkeypatch.setattr(v1_preflight, "_load_env", lambda _: env)
        monkeypatch.setattr(v1_preflight.importlib.util, "find_spec", lambda _: object())
        monkeypatch.setattr(v1_preflight.shutil, "which", lambda _: "fixture")
        monkeypatch.setattr(v1_preflight, "_cuda_check", lambda: v1_preflight._check("gpu:cuda", "pass", "fixture"))
        monkeypatch.setattr(
            v1_preflight,
            "_optional_ocr_check",
            lambda root, values, timeout_seconds=None: v1_preflight._check("ocr", "pass", "fixture"),
        )
        monkeypatch.setattr(v1_preflight, "_tts_check", lambda _: v1_preflight._check("tts", "pass", "fixture"))
        monkeypatch.setattr(
            v1_preflight,
            "_directory_check",
            lambda name, path, read_only=False: v1_preflight._check(name, "pass", "fixture"),
        )
        return v1_preflight.run_preflight(tmp_path, "telegram")

    empty = configure_preflight({"PIPELINE_MODE": "legacy", "BOT_TOKEN": "fixture"})
    empty_check = next(item for item in empty["checks"] if item["name"] == "secret:TELEGRAM_ALLOWED_CHAT_IDS")
    assert empty_check["status"] == "warning"
    assert empty_check["message"] == "empty; bot is open to everyone"

    configured = configure_preflight({
        "PIPELINE_MODE": "legacy",
        "BOT_TOKEN": "fixture",
        "TELEGRAM_ALLOWED_CHAT_IDS": "1, 2, -3, 2",
    })
    configured_check = next(
        item for item in configured["checks"] if item["name"] == "secret:TELEGRAM_ALLOWED_CHAT_IDS"
    )
    assert configured_check["status"] == "pass"
    assert configured_check["message"] == "configured (3 ids)"

    monkeypatch.setenv("TELEGRAM_ALLOWED_CHAT_IDS", "55")
    os_fallback = configure_preflight({"PIPELINE_MODE": "legacy", "BOT_TOKEN": "fixture"})
    fallback_check = next(
        item for item in os_fallback["checks"] if item["name"] == "secret:TELEGRAM_ALLOWED_CHAT_IDS"
    )
    assert fallback_check["status"] == "pass"
    assert fallback_check["message"] == "configured (1 ids)"
