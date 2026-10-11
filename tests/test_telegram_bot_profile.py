import os
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from backend import telegram_bot


class TelegramBotProfileTests(unittest.IsolatedAsyncioTestCase):
    async def test_start_message_identifies_tool_v1_and_voice_command(self):
        reply = AsyncMock()
        await telegram_bot.cmd_start(SimpleNamespace(message=SimpleNamespace(reply_text=reply)), None)
        text = reply.call_args.args[0]
        self.assertIn("Tool V1", text)
        self.assertIn("/voice_auto", text)
        self.assertNotIn("Bot V2", text)

    def test_placeholder_credentials_are_rejected(self):
        for value in ("YOUR_TOKEN", "PASTE_TOKEN", ""):
            with self.subTest(value=value), patch.dict(os.environ, {"TEST_BOT_SECRET": value}):
                self.assertEqual(telegram_bot.configured_secret("TEST_BOT_SECRET"), "")

    def test_explicit_credential_is_preserved(self):
        with patch.dict(os.environ, {"TEST_BOT_SECRET": "test-only-token"}):
            self.assertEqual(telegram_bot.configured_secret("TEST_BOT_SECRET"), "test-only-token")

    def test_polling_registers_v1_handlers_and_keeps_pending_updates(self):
        source = telegram_bot.Path(telegram_bot.__file__).read_text(encoding="utf-8")
        self.assertIn('CommandHandler("voice_auto", cmd_voice_auto)', source)
        self.assertIn('CommandHandler("stop", cmd_stop)', source)
        self.assertIn("drop_pending_updates=False", source)
        self.assertIn("install_tool_control(app, globals(), 'v1')", source)


if __name__ == "__main__":
    unittest.main()
