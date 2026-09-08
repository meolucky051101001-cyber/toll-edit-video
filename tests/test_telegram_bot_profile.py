import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from backend import telegram_bot


class TelegramBotProfileTests(unittest.IsolatedAsyncioTestCase):
    async def test_profile_is_published_for_expected_v2_identity(self):
        bot = SimpleNamespace(
            get_me=AsyncMock(return_value=SimpleNamespace(username="autodub_video_v2_bot")),
            set_my_commands=AsyncMock(),
            set_my_name=AsyncMock(),
            set_my_short_description=AsyncMock(),
            set_my_description=AsyncMock(),
        )
        with patch.object(telegram_bot, "BOT_EXPECTED_USERNAME", "autodub_video_v2_bot"):
            await telegram_bot.configure_bot_profile(SimpleNamespace(bot=bot))

        bot.set_my_commands.assert_awaited_once_with(telegram_bot.BOT_COMMANDS)
        bot.set_my_name.assert_awaited_once_with("AutoDub Video Bot V2")

    async def test_v1_token_is_rejected_before_polling(self):
        bot = SimpleNamespace(
            get_me=AsyncMock(return_value=SimpleNamespace(username="autodub_video_bot")),
        )
        with patch.object(telegram_bot, "BOT_EXPECTED_USERNAME", "autodub_video_v2_bot"):
            with self.assertRaisesRegex(RuntimeError, "expected @autodub_video_v2_bot"):
                await telegram_bot.configure_bot_profile(SimpleNamespace(bot=bot))

    async def test_transient_polling_error_is_handled_as_warning(self):
        error = telegram_bot.NetworkError("temporary read failure")
        with patch.object(telegram_bot.logger, "warning") as warning:
            await telegram_bot.telegram_error_handler(
                None,
                SimpleNamespace(error=error),
            )
        warning.assert_called_once()

    def test_polling_uses_dedicated_request_and_error_handler(self):
        source = telegram_bot.Path(telegram_bot.__file__).read_text(encoding="utf-8")
        self.assertIn(".get_updates_request(get_updates_request)", source)
        self.assertIn("app.add_error_handler(telegram_error_handler)", source)


if __name__ == "__main__":
    unittest.main()
