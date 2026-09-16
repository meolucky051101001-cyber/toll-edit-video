from backend.core.config import Settings
from backend.providers.ai.base import AIError, AIProvider
from backend.providers.ai.gemini import GeminiAIProvider


def get_ai_provider(config: Settings) -> AIProvider:
    if config.ai_provider == "gemini":
        return GeminiAIProvider(config)
    raise AIError("disabled", "AI chưa được bật. Đang dùng query gốc.")
