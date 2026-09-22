from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from backend.services.douyin_browser_service import DouyinBrowserService


class MockLocator:
    def __init__(self, count_val: int = 1, is_visible_val: bool = True):
        self._count = count_val
        self._visible = is_visible_val

    async def count(self) -> int:
        return self._count

    async def is_visible(self) -> bool:
        return self._visible

    async def click(self, timeout: int = 800) -> None:
        pass

    async def screenshot(self, path: str) -> None:
        # Create a non-empty fake image file
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"\x89PNG\r\n\x1a\nfake_image_data")

    @property
    def first(self) -> "MockLocator":
        return self


class MockPage:
    def __init__(self, slide_images: list[str], loop_back: bool = True):
        self.slide_images = slide_images
        self.loop_back = loop_back
        self.current_index = 0
        self.captured_locators: list[str] = []
        self.keyboard = AsyncMock()

    def is_closed(self) -> bool:
        return False

    async def close(self) -> None:
        pass

    async def evaluate(self, script: str) -> str | None:
        if not self.slide_images:
            return None
        # Return the current active slide image
        idx = self.current_index % len(self.slide_images)
        src = self.slide_images[idx]
        return src

    def locator(self, selector: str) -> MockLocator:
        self.captured_locators.append(selector)
        # Advance slide on next_btn click simulation
        if "arrow-right" in selector or "next" in selector:
            mock_btn = MockLocator(count_val=1, is_visible_val=True)

            async def advance_slide(timeout: int = 800) -> None:
                if self.loop_back or self.current_index < len(self.slide_images) - 1:
                    self.current_index += 1

            mock_btn.click = advance_slide  # type: ignore[method-assign]
            return mock_btn
        return MockLocator(count_val=1, is_visible_val=True)

    async def screenshot(self, path: str) -> None:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"\x89PNG\r\n\x1a\nfallback_image_data")


@pytest.mark.anyio
async def test_carousel_single_image(tmp_path: Path):
    """Simulates a 1-image album, verifying exactly 1 file is saved with no duplicates."""
    service = DouyinBrowserService(profile=tmp_path / "profile", headless=True)
    mock_page = MockPage(slide_images=["https://p3.douyinpic.com/img1.jpeg"])

    saved = await service.screenshot_note_images(
        note_url="https://www.douyin.com/note/11111",
        save_dir=tmp_path / "single_album",
        page=mock_page,  # type: ignore[arg-type]
        max_slides=5,
    )

    assert len(saved) == 1
    assert saved[0].exists()
    assert saved[0].name == "001.png"
    # Ensure locator specifically looked for active slide
    assert any("data-current-active-slide" in loc for loc in mock_page.captured_locators)


@pytest.mark.anyio
async def test_carousel_three_images(tmp_path: Path):
    """Simulates a 3-image album, verifying 3 distinct files saved and loop termination."""
    service = DouyinBrowserService(profile=tmp_path / "profile", headless=True)
    images = [
        "https://p3.douyinpic.com/slide1.jpeg",
        "https://p3.douyinpic.com/slide2.jpeg",
        "https://p3.douyinpic.com/slide3.jpeg",
    ]
    mock_page = MockPage(slide_images=images, loop_back=True)

    saved = await service.screenshot_note_images(
        note_url="https://www.douyin.com/note/22222",
        save_dir=tmp_path / "three_album",
        page=mock_page,  # type: ignore[arg-type]
        max_slides=10,
    )

    assert len(saved) == 3
    names = [p.name for p in saved]
    assert names == ["001.png", "002.png", "003.png"]
    # All files exist and non-empty
    for p in saved:
        assert p.exists()
        assert p.stat().st_size > 0


@pytest.mark.anyio
async def test_carousel_five_images(tmp_path: Path):
    """Simulates a 5-image album, verifying 5 distinct files saved in order without duplicates."""
    service = DouyinBrowserService(profile=tmp_path / "profile", headless=True)
    images = [
        f"https://p3.douyinpic.com/photo_{i}.jpeg" for i in range(1, 6)
    ]
    mock_page = MockPage(slide_images=images, loop_back=True)

    saved = await service.screenshot_note_images(
        note_url="https://www.douyin.com/note/33333",
        save_dir=tmp_path / "five_album",
        page=mock_page,  # type: ignore[arg-type]
        max_slides=12,
    )

    assert len(saved) == 5
    names = [p.name for p in saved]
    assert names == ["001.png", "002.png", "003.png", "004.png", "005.png"]
    assert len(set(saved)) == 5
