"""Shared access controls for Telegram bots."""

from __future__ import annotations

import logging
import os
import re
from pathlib import Path


logger = logging.getLogger(__name__)


def parse_chat_ids(raw: str) -> frozenset[int]:
    value = raw or ""
    tokens = re.findall(r"-?\d+", value)
    if value.strip() and not tokens:
        raise ValueError("TELEGRAM_ALLOWED_CHAT_IDS is set but contains no numeric chat id")
    return frozenset(int(token) for token in tokens)


def load_allowed_chat_ids(
    name: str = "TELEGRAM_ALLOWED_CHAT_IDS",
    env_file: Path | None = None,
) -> frozenset[int]:
    value = os.environ.get(name, "")
    if value.strip():
        return parse_chat_ids(value)
    if env_file is not None:
        try:
            for line in Path(env_file).read_text(encoding="utf-8").splitlines():
                if line.strip().startswith(f"{name}="):
                    return parse_chat_ids(line.split("=", 1)[1].strip().strip('"').strip("'"))
        except OSError:
            pass
    return frozenset()


def is_update_allowed(update, allowed: frozenset[int]) -> bool:
    if not allowed:
        return True
    chat_id = getattr(getattr(update, "effective_chat", None), "id", None)
    user_id = getattr(getattr(update, "effective_user", None), "id", None)
    return chat_id in allowed or user_id in allowed


def install_access_gate(application, allowed: frozenset[int], *, bot_name: str) -> bool:
    if not allowed:
        logger.warning(
            "%s Telegram bot is open to everyone; set TELEGRAM_ALLOWED_CHAT_IDS to restrict access",
            bot_name,
        )
        return False

    from telegram import Update
    from telegram.ext import ApplicationHandlerStop, TypeHandler

    async def gate(update, context):
        if is_update_allowed(update, allowed):
            return
        chat_id = getattr(getattr(update, "effective_chat", None), "id", None)
        user_id = getattr(getattr(update, "effective_user", None), "id", None)
        logger.warning(
            "%s Telegram access denied: chat_id=%s user_id=%s",
            bot_name,
            chat_id,
            user_id,
        )
        message = getattr(update, "effective_message", None)
        if message is not None:
            try:
                await message.reply_text(
                    f"⛔ Bot này chỉ dành cho chủ máy. Chat ID của bạn: {chat_id}"
                )
            except Exception:
                pass
        raise ApplicationHandlerStop

    application.add_handler(TypeHandler(Update, gate), group=-2000)
    return True
