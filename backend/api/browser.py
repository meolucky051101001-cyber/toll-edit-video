import asyncio

from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel

from backend.providers.base import ProviderUnavailableError

router = APIRouter(prefix="/api/browser/xiaohongshu", tags=["browser"])
douyin_router = APIRouter(prefix="/api/browser/douyin", tags=["browser"])


class ClickPayload(BaseModel):
    x: float
    y: float


class DragPayload(BaseModel):
    from_x: float
    from_y: float
    to_x: float
    to_y: float


@router.get("")
async def status(request: Request):
    return await request.app.state.browser.refresh_status()


@router.post("/open")
async def open_browser(request: Request):
    try:
        return await request.app.state.browser.open()
    except ProviderUnavailableError as error:
        raise HTTPException(503, str(error)) from None


@router.post("/close")
async def close_browser(request: Request):
    browser = request.app.state.browser
    if browser.lock.locked():
        raise HTTPException(409, "Hãy hủy lượt tìm đang chạy trước khi đóng trình duyệt.")
    await browser.close()
    return browser.snapshot()


@router.get("/screenshot")
async def get_screenshot(request: Request, crop: str | None = None):
    browser = request.app.state.browser
    try:
        page = browser.get_active_page()
    except Exception:
        raise HTTPException(404, "Trình duyệt chưa mở.")
    try:
        if crop == "qr":
            qr_elem = page.locator(
                ".qrcode-img:visible, [class*='qrcode'] img:visible, [class*='qrcode']:visible, canvas:visible"
            ).first
            if await qr_elem.count() > 0:
                box = await qr_elem.bounding_box()
                if box and box["width"] >= 60 and box["height"] >= 60:
                    clip = {
                        "x": max(0.0, box["x"] - 25),
                        "y": max(0.0, box["y"] - 25),
                        "width": min(box["width"] + 50, 1280 - max(0.0, box["x"] - 25)),
                        "height": min(box["height"] + 50, 900 - max(0.0, box["y"] - 25)),
                    }
                    img_bytes = await page.screenshot(clip=clip)
                    return Response(
                        content=img_bytes,
                        media_type="image/png",
                        headers={"Cache-Control": "no-cache, no-store, must-revalidate"},
                    )
            clip = {"x": 300, "y": 220, "width": 320, "height": 340}
            img_bytes = await page.screenshot(clip=clip)
            return Response(
                content=img_bytes,
                media_type="image/png",
                headers={"Cache-Control": "no-cache, no-store, must-revalidate"},
            )

        img_bytes = await page.screenshot()
        return Response(
            content=img_bytes,
            media_type="image/png",
            headers={"Cache-Control": "no-cache, no-store, must-revalidate"},
        )
    except Exception:
        raise HTTPException(503, "Không chụp được màn hình trình duyệt.")


@router.post("/refresh_qr")
async def refresh_xiaohongshu_qr(request: Request):
    browser = request.app.state.browser
    return await browser.refresh_qr()


@router.post("/click")
async def click_xiaohongshu(payload: ClickPayload, request: Request):
    browser = request.app.state.browser
    try:
        page = browser.get_active_page()
    except Exception:
        raise HTTPException(404, "Trình duyệt chưa mở.")
    try:
        await page.mouse.click(payload.x, payload.y)
        await asyncio.sleep(0.5)
        await browser.inspect(page)
        return browser.snapshot()
    except Exception as e:
        raise HTTPException(500, f"Lỗi khi click: {e}")


@router.post("/drag")
async def drag_xiaohongshu(payload: DragPayload, request: Request):
    browser = request.app.state.browser
    try:
        page = browser.get_active_page()
    except Exception:
        raise HTTPException(404, "Trình duyệt chưa mở.")
    try:
        await page.mouse.move(payload.from_x, payload.from_y)
        await page.mouse.down()
        await page.mouse.move(payload.to_x, payload.to_y, steps=10)
        await page.mouse.up()
        await asyncio.sleep(0.5)
        await browser.inspect(page)
        return browser.snapshot()
    except Exception as e:
        raise HTTPException(500, f"Lỗi khi kéo trượt: {e}")


@douyin_router.get("")
async def douyin_status(request: Request):
    return await request.app.state.douyin_browser.refresh_status()


@douyin_router.post("/open")
async def open_douyin_browser(request: Request):
    try:
        return await request.app.state.douyin_browser.open()
    except ProviderUnavailableError as error:
        raise HTTPException(503, str(error)) from None


@douyin_router.post("/close")
async def close_douyin_browser(request: Request):
    browser = request.app.state.douyin_browser
    if browser.lock.locked():
        raise HTTPException(409, "Hãy hủy lượt tìm đang chạy trước khi đóng trình duyệt.")
    await browser.close()
    return browser.snapshot()


@douyin_router.get("/screenshot")
async def get_douyin_screenshot(request: Request, crop: str | None = None):
    browser = request.app.state.douyin_browser
    try:
        page = browser.get_active_page()
    except Exception:
        raise HTTPException(404, "Trình duyệt chưa mở.")
    try:
        if crop == "qr":
            qr_elem = page.locator(
                "[data-e2e='qrcode-image']:visible, .qrcode-image:visible, [class*='qrcode'] img:visible, img[src*='qrcode']:visible, #login-panel [class*='qrcode']:visible, #login-panel canvas:visible"
            ).first
            if await qr_elem.count() > 0:
                box = await qr_elem.bounding_box()
                if box and box["width"] >= 60 and box["height"] >= 60:
                    clip = {
                        "x": max(0.0, box["x"] - 25),
                        "y": max(0.0, box["y"] - 25),
                        "width": min(box["width"] + 50, 1280 - max(0.0, box["x"] - 25)),
                        "height": min(box["height"] + 50, 900 - max(0.0, box["y"] - 25)),
                    }
                    img_bytes = await page.screenshot(clip=clip)
                    return Response(
                        content=img_bytes,
                        media_type="image/png",
                        headers={"Cache-Control": "no-cache, no-store, must-revalidate"},
                    )
            clip = {"x": 420, "y": 180, "width": 440, "height": 480}
            img_bytes = await page.screenshot(clip=clip)
            return Response(
                content=img_bytes,
                media_type="image/png",
                headers={"Cache-Control": "no-cache, no-store, must-revalidate"},
            )

        img_bytes = await page.screenshot()
        return Response(
            content=img_bytes,
            media_type="image/png",
            headers={"Cache-Control": "no-cache, no-store, must-revalidate"},
        )
    except Exception:
        raise HTTPException(503, "Không chụp được màn hình trình duyệt.")


@router.get("/inspect_cards")
async def inspect_cards(request: Request):
    browser = request.app.state.browser
    try:
        page = browser.get_active_page()
    except Exception:
        raise HTTPException(404, "Trình duyệt chưa mở.")
    return await page.evaluate("""() => {
        const cards = document.querySelectorAll('section.note-item, div.note-item, .search-result-item, .card, [class*="note"], [class*="NoteItem"]');
        return Array.from(cards).map((c, idx) => {
            const anchors = Array.from(c.querySelectorAll('a')).map(a => ({
                className: a.className,
                href: a.getAttribute('href'),
                rawHref: a.href,
            }));
            const title = c.querySelector('.title, [class*="title"], .desc, [class*="desc"]')?.innerText?.trim();
            return { index: idx, title, anchors };
        });
    }""")


@douyin_router.post("/refresh_qr")
async def refresh_douyin_qr(request: Request):
    browser = request.app.state.douyin_browser
    return await browser.refresh_qr()


@douyin_router.post("/click")
async def click_douyin(payload: ClickPayload, request: Request):
    browser = request.app.state.douyin_browser
    try:
        page = browser.get_active_page()
    except Exception:
        raise HTTPException(404, "Trình duyệt chưa mở.")
    try:
        await page.mouse.click(payload.x, payload.y)
        await asyncio.sleep(0.5)
        await browser.inspect(page)
        return browser.snapshot()
    except Exception as e:
        raise HTTPException(500, f"Lỗi khi click: {e}")


@douyin_router.post("/drag")
async def drag_douyin(payload: DragPayload, request: Request):
    browser = request.app.state.douyin_browser
    try:
        page = browser.get_active_page()
    except Exception:
        raise HTTPException(404, "Trình duyệt chưa mở.")
    try:
        await page.mouse.move(payload.from_x, payload.from_y)
        await page.mouse.down()
        await page.mouse.move(payload.to_x, payload.to_y, steps=10)
        await page.mouse.up()
        await asyncio.sleep(0.5)
        await browser.inspect(page)
        return browser.snapshot()
    except Exception as e:
        raise HTTPException(500, f"Lỗi khi kéo trượt: {e}")


