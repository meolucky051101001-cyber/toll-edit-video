from abc import ABC, abstractmethod


class AIError(Exception):
    def __init__(self, code: str, message: str, retryable: bool = False):
        super().__init__(message)
        self.code = code
        self.retryable = retryable


class AIProvider(ABC):
    name: str

    @abstractmethod
    async def expand(self, query: str) -> str:
        """Return raw structured JSON; business logic validates the payload."""
        raise NotImplementedError

    async def analyze_script(self, video_data: dict) -> str:
        """Return raw structured JSON for script analysis."""
        raise NotImplementedError
