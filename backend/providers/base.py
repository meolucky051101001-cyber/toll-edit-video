from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable

from backend.schemas.contracts import SearchFilters, VideoResult


class ProviderError(Exception):
    pass


class LoginRequiredError(ProviderError):
    pass


class CaptchaRequiredError(ProviderError):
    pass


class RateLimitedError(ProviderError):
    pass


class SelectorChangedError(ProviderError):
    pass


class NetworkError(ProviderError):
    pass


class ProviderTimeoutError(ProviderError):
    pass


class ProviderUnavailableError(ProviderError):
    pass


class SearchProvider(ABC):
    @abstractmethod
    async def search(
        self,
        query: str,
        limit: int,
        filters: SearchFilters,
        on_batch: Callable[[list[VideoResult]], Awaitable[None]] | None = None,
    ) -> list[VideoResult]:
        raise NotImplementedError

    async def close(self) -> None:
        pass


class UserActionRequired(ProviderError):
    def __init__(self, state: str, message: str, partial: list[VideoResult] | None = None):
        super().__init__(message)
        self.state = state
        self.partial = partial or []
