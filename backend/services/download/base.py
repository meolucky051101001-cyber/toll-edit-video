from abc import ABC, abstractmethod

from backend.models.video import Video
from backend.schemas.downloads import ResolvedMedia


class DownloadAdapterError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


class BaseDownloadAdapter(ABC):
    @property
    @abstractmethod
    def name(self) -> str:
        ...

    @property
    @abstractmethod
    def version(self) -> str:
        ...

    @abstractmethod
    async def resolve(self, video: Video) -> ResolvedMedia:
        """Resolves fresh media URL and headers for the given video."""
        ...

    @abstractmethod
    async def health_check(self) -> bool:
        """Checks whether the upstream local service is accessible."""
        ...
