from unittest.mock import AsyncMock, MagicMock

import pytest

from backend.services.douyin_browser_service import DouyinBrowserService


class FakeElement:
    def __init__(self, width: float, height: float, visible: bool = True, opacity: float = 1.0):
        self.width = width
        self.height = height
        self.visible = visible
        self.opacity = opacity

    async def is_visible(self) -> bool:
        return self.visible

    async def bounding_box(self) -> dict[str, float]:
        return {"x": 0, "y": 0, "width": self.width, "height": self.height}

    async def evaluate(self, _expression: str) -> float:
        return self.opacity


class FakeLocator:
    def __init__(self, elements: list[FakeElement] | None = None):
        self.elements = elements or []
        self.first = self

    async def all(self) -> list[FakeElement]:
        return self.elements

    async def count(self) -> int:
        return len(self.elements)

    async def inner_text(self, timeout: int | None = None) -> str:
        return ""

    async def input_value(self) -> str:
        return ""


class FakeFrame:
    def __init__(self, url: str, element: FakeElement):
        self.url = url
        self._element = element

    async def frame_element(self) -> FakeElement:
        return self._element


@pytest.mark.anyio
async def test_douyin_ignores_hidden_captcha_sdk_and_generic_verify_iframe(tmp_path):
    service = DouyinBrowserService(profile=tmp_path / "douyin", headless=True)
    page = MagicMock()
    page.locator = MagicMock(side_effect=lambda _selector: FakeLocator())
    page.frames = [
        FakeFrame("https://sec.douyin.com/captcha/sdk", FakeElement(1, 1, visible=False)),
        FakeFrame("https://verify.douyin.com/sdk", FakeElement(300, 200)),
    ]

    assert await service._visible_captcha_evidence(page) is None


@pytest.mark.anyio
async def test_douyin_detects_rendered_captcha_container(tmp_path):
    service = DouyinBrowserService(profile=tmp_path / "douyin", headless=True)
    challenge = FakeElement(320, 180)
    page = MagicMock()
    page.locator = MagicMock(
        side_effect=lambda selector: FakeLocator([challenge])
        if selector == ".captcha-verify-container"
        else FakeLocator()
    )
    page.frames = []

    assert await service._visible_captcha_evidence(page) == (
        "visible_challenge_element:.captcha-verify-container"
    )


@pytest.mark.anyio
async def test_douyin_detects_visible_captcha_iframe_but_not_hidden_one(tmp_path):
    service = DouyinBrowserService(profile=tmp_path / "douyin", headless=True)
    page = MagicMock()
    page.locator = MagicMock(side_effect=lambda _selector: FakeLocator())
    page.frames = [
        FakeFrame(
            "https://rc-verifycenter.example.test/nocaptcha",
            FakeElement(280, 180, visible=True),
        )
    ]

    assert await service._visible_captcha_evidence(page) == "visible_captcha_iframe"


@pytest.mark.anyio
async def test_douyin_home_with_unrelated_verification_word_is_searchable(tmp_path):
    service = DouyinBrowserService(profile=tmp_path / "douyin", headless=True)
    context = MagicMock()
    context.cookies = AsyncMock(return_value=[])
    context.storage_state = AsyncMock()
    service.context = context

    class Page:
        url = "https://www.douyin.com/jingxuan"
        frames = [
            FakeFrame("https://sec.douyin.com/captcha/sdk", FakeElement(1, 1, visible=False))
        ]

        def is_closed(self) -> bool:
            return False

        async def title(self) -> str:
            return "抖音精选电脑版 - 抖音旗下优质视频平台"

        def locator(self, selector: str) -> FakeLocator:
            if selector == "body":
                body = FakeLocator()
                body.inner_text = AsyncMock(return_value="精选视频推荐，verification SDK loaded")
                return body
            return FakeLocator()

    state = await service.inspect(Page())

    assert state == "page_available"
    assert service.diagnostics["captcha_visible"] is False
    assert service.diagnostics["captcha_detection_reason"] is None


@pytest.mark.anyio
async def test_open_brings_headful_challenge_page_to_front(tmp_path):
    service = DouyinBrowserService(profile=tmp_path / "douyin", headless=True)
    service.is_currently_headless = True
    page = MagicMock()
    page.is_closed = MagicMock(return_value=False)
    page.url = "https://www.douyin.com/search/example?type=video"
    page.bring_to_front = AsyncMock()
    service.page = page
    service.elevate_to_headful = AsyncMock(return_value=page)
    service.inspect = AsyncMock(return_value="verification_required")
    service.state = "verification_required"

    snapshot = await service.open()

    page.bring_to_front.assert_awaited_once()
    service.inspect.assert_awaited_once_with(page)
    assert snapshot["active_url"] == page.url
