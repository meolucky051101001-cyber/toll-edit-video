import asyncio
import math
import random
from io import BytesIO

from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel

from backend.providers.base import ProviderUnavailableError
from backend.services.browser_session import save_session_checkpoint

router = APIRouter(prefix="/api/browser/xiaohongshu", tags=["browser"])
douyin_router = APIRouter(prefix="/api/browser/douyin", tags=["browser"])


async def human_drag(page, from_x: float, from_y: float, to_x: float, to_y: float) -> None:
    """Simulate human-like mouse drag with cubic easing, micro-delays, and subtle jitter."""
    await page.mouse.move(from_x, from_y)
    await asyncio.sleep(random.uniform(0.08, 0.12))
    await page.mouse.down()
    await asyncio.sleep(random.uniform(0.06, 0.10))

    dx = to_x - from_x
    dy = to_y - from_y
    steps = random.randint(28, 36)

    for i in range(1, steps + 1):
        t = i / steps
        # Smooth S-curve easing
        progress = t * t * (3.0 - 2.0 * t)
        curr_x = from_x + dx * progress
        jitter_y = math.sin(t * math.pi * 3) * random.uniform(0.3, 0.9)
        curr_y = from_y + dy * progress + jitter_y
        await page.mouse.move(curr_x, curr_y)
        delay = random.uniform(0.012, 0.020) if 0.2 < t < 0.8 else random.uniform(0.022, 0.035)
        await asyncio.sleep(delay)

    await asyncio.sleep(random.uniform(0.12, 0.20))
    await page.mouse.up()



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
    browser = request.app.state.browser
    if browser.lock.locked():
        raise HTTPException(409, "Không thể mở lại trình duyệt khi tìm kiếm đang chạy.")
    try:
        return await browser.open()
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
                ".qrcode-img:visible, [class*='qrcode'] img:visible, [class*='qrcode']:visible"
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
            raise HTTPException(
                404,
                "Xiaohongshu chưa hiển thị mã QR đăng nhập. Hãy mở cửa sổ trình duyệt để kiểm tra trực tiếp.",
            )

        img_bytes = await page.screenshot()
        return Response(
            content=img_bytes,
            media_type="image/png",
            headers={"Cache-Control": "no-cache, no-store, must-revalidate"},
        )
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(503, "Không chụp được màn hình trình duyệt.")


@router.post("/refresh_qr")
async def refresh_xiaohongshu_qr(request: Request):
    browser = request.app.state.browser
    if browser.lock.locked():
        raise HTTPException(409, "Trình duyệt đang được sử dụng bởi tìm kiếm.")
    return await browser.refresh_qr()


@router.post("/click")
async def click_xiaohongshu(payload: ClickPayload, request: Request):
    browser = request.app.state.browser
    if browser.lock.locked():
        raise HTTPException(409, "Trình duyệt đang được sử dụng bởi tìm kiếm.")
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
    if browser.lock.locked():
        raise HTTPException(409, "Trình duyệt đang được sử dụng bởi tìm kiếm.")
    try:
        page = browser.get_active_page()
    except Exception:
        raise HTTPException(404, "Trình duyệt chưa mở.")
    try:
        await human_drag(page, payload.from_x, payload.from_y, payload.to_x, payload.to_y)
        await asyncio.sleep(0.8)
        await browser.inspect(page)
        if browser.context and browser.state in {"authenticated", "page_available"}:
            try:
                await save_session_checkpoint(browser.context, browser.profile)
            except Exception:
                pass
        return browser.snapshot()
    except Exception as e:
        raise HTTPException(500, f"Lỗi khi kéo trượt: {e}")



@douyin_router.get("")
async def douyin_status(request: Request):
    return await request.app.state.douyin_browser.refresh_status()


@router.post("/refresh_verification")
@douyin_router.post("/refresh_verification")
async def refresh_verification(request: Request):
    """Reload the actual blocked tab, without clearing the saved login."""
    browser = (
        request.app.state.douyin_browser
        if "/douyin/" in request.url.path
        else request.app.state.browser
    )
    if browser.lock.locked():
        raise HTTPException(409, "Trình duyệt đang tìm kiếm. Hãy chờ trạng thái yêu cầu xác minh.")
    async with browser.lock:
        try:
            page = browser.get_active_page()
        except ProviderUnavailableError:
            raise HTTPException(404, "Trình duyệt chưa mở.") from None
        if browser.state != "verification_required":
            raise HTTPException(409, "Trang hiện tại không yêu cầu CAPTCHA. Hãy kiểm tra trạng thái hoặc tiếp tục lượt tìm.")
        try:
            await page.reload(wait_until="domcontentloaded", timeout=30000)
            await browser.inspect(page)
            return browser.snapshot()
        except Exception:
            raise HTTPException(503, "Trang xác minh chưa tải được. Kiểm tra kết nối rồi thử lại; phiên đăng nhập được giữ nguyên.") from None


@douyin_router.post("/open")
async def open_douyin_browser(request: Request):
    browser = request.app.state.douyin_browser
    if browser.lock.locked():
        raise HTTPException(409, "Không thể mở lại trình duyệt khi tìm kiếm đang chạy.")
    try:
        return await browser.open()
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
            raise HTTPException(
                404,
                "Douyin chưa hiển thị mã QR đăng nhập. Hãy mở cửa sổ trình duyệt để kiểm tra trực tiếp.",
            )

        img_bytes = await page.screenshot()
        return Response(
            content=img_bytes,
            media_type="image/png",
            headers={"Cache-Control": "no-cache, no-store, must-revalidate"},
        )
    except HTTPException:
        raise
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
            const mediaClasses = Array.from(c.querySelectorAll('svg, span, div, video'))
                .map(el => ({ tag: el.tagName, className: typeof el.className === 'string' ? el.className : el.className?.baseVal || '' }))
                .filter(el => /play|video|duration|cover|badge|type|mask|note/i.test(el.className))
                .slice(0, 20);
            return { index: idx, title, anchors, mediaClasses };
        });
    }""")
@douyin_router.get("/inspect_frames")
async def inspect_douyin_frames(request: Request):
    browser = request.app.state.douyin_browser
    try:
        page = browser.get_active_page()
    except Exception:
        raise HTTPException(404, "Trình duyệt chưa mở.")

    frames_info = []
    for idx, frame in enumerate(getattr(page, "frames", [])):
        info = {
            "index": idx,
            "name": frame.name,
            "url": frame.url,
        }
        try:
            elem = await frame.frame_element()
            info["visible"] = await elem.is_visible()
            info["box"] = await elem.bounding_box()
        except Exception as e:
            info["elem_err"] = str(e)
        
        # Test finding slider buttons
        for sel in [".secsdk-captcha-drag-icon", "#secsdk-captcha-drag-wrapper", "[class*='drag-icon']", "[class*='slider-btn']", "#captcha-verify-image", "img"]:
            try:
                count = await frame.locator(sel).count()
                if count > 0:
                    info[f"has_{sel}"] = count
                    b = await frame.locator(sel).first.bounding_box()
                    info[f"box_{sel}"] = b
            except Exception:
                pass
        frames_info.append(info)
    return {"active_url": page.url, "frames": frames_info}
@douyin_router.post("/refresh_qr")
async def refresh_douyin_qr(request: Request):
    browser = request.app.state.douyin_browser
    if browser.lock.locked():
        raise HTTPException(409, "Trình duyệt đang được sử dụng bởi tìm kiếm.")
    return await browser.refresh_qr()


@douyin_router.post("/click")
async def click_douyin(payload: ClickPayload, request: Request):
    browser = request.app.state.douyin_browser
    if browser.lock.locked():
        raise HTTPException(409, "Trình duyệt đang được sử dụng bởi tìm kiếm.")
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
    if browser.lock.locked():
        raise HTTPException(409, "Trình duyệt đang được sử dụng bởi tìm kiếm.")
    try:
        page = browser.get_active_page()
    except Exception:
        raise HTTPException(404, "Trình duyệt chưa mở.")
    try:
        await human_drag(page, payload.from_x, payload.from_y, payload.to_x, payload.to_y)
        await asyncio.sleep(0.8)
        await browser.inspect(page)
        if browser.context and browser.state in {"authenticated", "page_available"}:
            try:
                await save_session_checkpoint(browser.context, browser.profile)
            except Exception:
                pass
        return browser.snapshot()
    except Exception as e:
        raise HTTPException(500, f"Lỗi khi kéo trượt: {e}")


def detect_puzzle_slider_distance(
    png_bytes: bytes,
    crop_box: tuple[int, int, int, int] | None = None,
) -> tuple[float, float, float] | None:
    """Accurately detects puzzle slider start and target positions using Pillow.
    Uses column clustering, vertical alignment, and bounding box similarity to match targets.
    Accepts an optional crop_box (x1, y1, x2, y2).
    Returns (from_x, from_y, to_x).
    """
    try:
        from PIL import Image

        img = Image.open(BytesIO(png_bytes)).convert("RGB")
        w, h = img.size

        # 1. Bounding box of the puzzle image
        if crop_box:
            crop_x1, crop_y1, crop_x2, crop_y2 = crop_box
            crop_x1 = max(0, min(w - 10, crop_x1))
            crop_y1 = max(0, min(h - 10, crop_y1))
            crop_x2 = max(crop_x1 + 50, min(w, crop_x2))
            crop_y2 = max(crop_y1 + 50, min(h, crop_y2))
        else:
            crop_x1, crop_y1, crop_x2, crop_y2 = 470, 325, 810, 537
        crop = img.crop((crop_x1, crop_y1, crop_x2, crop_y2))

        # 2. Detect left puzzle piece with bright white outline
        left_white: list[tuple[int, int]] = []
        for thresh in (225, 205, 185):
            left_white = []
            max_x_search = min(int(crop.width * 0.35), 110)
            for x in range(5, max_x_search):
                for y in range(8, crop.height - 8):
                    pix = crop.getpixel((x, y))
                    if isinstance(pix, (tuple, list)) and len(pix) >= 3:
                        r, g, b = int(pix[0]), int(pix[1]), int(pix[2])
                        if r > thresh and g > thresh and b > thresh:
                            left_white.append((x, y))
            if len(left_white) >= 12:
                break

        if not left_white:
            return None

        lx = [p[0] for p in left_white]
        ly = [p[1] for p in left_white]
        pw = max(lx) - min(lx)
        ph = max(ly) - min(ly)
        pcx = (min(lx) + max(lx)) / 2.0
        pcy = (min(ly) + max(ly)) / 2.0

        # 3. Collect dark columns to the right of the starting piece
        dark_cols: dict[int, list[int]] = {}
        min_search_x = int(pcx + pw / 2.0 + 12)
        for thresh_dark in (85, 100, 115):
            dark_cols = {}
            for x in range(min_search_x, crop.width - 8):
                dark_in_col = []
                for y in range(8, crop.height - 8):
                    pix = crop.getpixel((x, y))
                    if isinstance(pix, (tuple, list)) and len(pix) >= 3:
                        r, g, b = int(pix[0]), int(pix[1]), int(pix[2])
                        if r < thresh_dark and g < thresh_dark and b < thresh_dark:
                            dark_in_col.append(y)
                if len(dark_in_col) >= 6:
                    dark_cols[x] = dark_in_col
            if len(dark_cols) >= 10:
                break

        if not dark_cols:
            return None

        # 4. Group adjacent columns into clusters
        clusters: list[list[int]] = []
        current_cluster: list[int] = []
        for x in sorted(dark_cols.keys()):
            if not current_cluster or x - current_cluster[-1] <= 6:
                current_cluster.append(x)
            else:
                clusters.append(current_cluster)
                current_cluster = [x]
        if current_cluster:
            clusters.append(current_cluster)

        # 5. Score candidate clusters by height match, y-alignment, and width match
        scored: list[tuple[float, float]] = []
        for cl in clusters:
            if len(cl) < 12:
                continue
            all_ys = []
            for x in cl:
                all_ys.extend(dark_cols[x])
            cw = max(cl) - min(cl)
            ch = max(all_ys) - min(all_ys)
            ccx = (min(cl) + max(cl)) / 2.0
            ccy = (min(all_ys) + max(all_ys)) / 2.0
            score = abs(ch - ph) * 2.0 + abs(ccy - pcy) * 3.0 + abs(cw - pw)
            dist = ccx - pcx
            if 25 <= dist <= (crop.width - 25):
                scored.append((score, dist))

        if not scored:
            return None

        scored.sort(key=lambda item: item[0])
        best_distance = scored[0][1]

        from_x = float(crop_x1 + 34)
        from_y = float(crop_y2 + 26)
        to_x = from_x + best_distance
        return from_x, from_y, to_x
    except Exception:
        return None


async def execute_slider_solve(page, browser) -> dict:
    """Finds slider element, computes exact travel distance, and executes human-like drag."""
    slider_locator = None
    track_box = None
    all_frames = [page] + getattr(page, "frames", [])

    # 1. Find slider button locator
    for frame in all_frames:
        for sel in (
            ".secsdk-captcha-drag-icon",
            "[class*='drag-icon']:not([class*='wrapper'])",
            "[class*='slider-btn']",
            ".captcha_verify_drag_btn",
            ".verify-move-block",
            ".nc_iconfont.btn_slide",
            ".nc_scale span[id*='n1z']",
            ".nc-container [class*='btn_slide']",
            ".geetest_slider_button",
            ".geetest_btn",
            "[class*='slide-btn']",
            "[class*='slider-button']",
            "[class*='drag_button']",
        ):
            try:
                loc = frame.locator(sel).first
                if await loc.count() > 0 and await loc.is_visible():
                    box = await loc.bounding_box()
                    if box and 18 <= box["width"] <= 90 and 18 <= box["height"] <= 90:
                        slider_locator = (loc, box)
                        break
            except Exception:
                pass
        if slider_locator:
            break

    # 2. Find track element if present
    for frame in all_frames:
        for track_sel in (
            ".nc_scale",
            "[class*='slider-track']",
            "[class*='drag-wrapper']",
            "[class*='verify-bar-area']",
            "[class*='verify-bar']",
            "[class*='secsdk-captcha-drag-wrapper']",
            "[class*='nc_container']",
        ):
            try:
                tloc = frame.locator(track_sel).first
                if await tloc.count() > 0 and await tloc.is_visible():
                    tbox = await tloc.bounding_box()
                    if tbox and tbox["width"] >= 160:
                        track_box = tbox
                        break
            except Exception:
                pass
        if track_box:
            break

    # 3. Find puzzle image element if present
    puzzle_crop = None
    for frame in all_frames:
        for img_sel in (
            "#captcha-verify-image",
            "img.captcha_verify_img_slide",
            "[class*='verify-img-panel'] img",
            "[class*='verify-img'] img",
            "[class*='captcha'] img",
            "[class*='puzzle'] img",
            "canvas[class*='captcha']",
            ".geetest_canvas_bg",
            "img[src*='captcha']",
            "img[src*='verify']",
        ):
            try:
                iloc = frame.locator(img_sel).first
                if await iloc.count() > 0 and await iloc.is_visible():
                    ibox = await iloc.bounding_box()
                    if ibox and ibox["width"] >= 160 and ibox["height"] >= 70:
                        puzzle_crop = (
                            int(ibox["x"]),
                            int(ibox["y"]),
                            int(ibox["x"] + ibox["width"]),
                            int(ibox["y"] + ibox["height"]),
                        )
                        break
            except Exception:
                pass
        if puzzle_crop:
            break

    img_bytes = await page.screenshot()
    coords = detect_puzzle_slider_distance(img_bytes, crop_box=puzzle_crop)

    from_x: float = 504.0
    from_y: float = 563.0
    to_x: float = 650.0

    if slider_locator:
        box = slider_locator[1]
        from_x = box["x"] + box["width"] / 2
        from_y = box["y"] + box["height"] / 2
        try:
            await slider_locator[0].hover()
            await asyncio.sleep(0.08)
        except Exception:
            pass

    if coords:
        from_c, _, to_c = coords
        travel = to_c - from_c
        to_x = from_x + travel
    elif track_box:
        # Simple track slider (NoCaptcha / Xiaohongshu slide-bar to right end)
        btn_w = slider_locator[1]["width"] if slider_locator else 36.0
        to_x = track_box["x"] + track_box["width"] - (btn_w * 0.5)
    elif slider_locator:
        # Fallback for standard slide bars when puzzle isn't cutout-based: drag 260px right
        to_x = from_x + 260.0
    else:
        raise HTTPException(422, "Không phát hiện được thanh trượt hoặc mảnh ghép trên trang.")

    await human_drag(page, from_x, from_y, to_x, from_y)
    await asyncio.sleep(1.5)
    await browser.inspect(page)

    solved = browser.state in {"authenticated", "page_available"}
    if browser.context and solved:
        try:
            await save_session_checkpoint(browser.context, browser.profile)
        except Exception:
            pass

    return {
        "ok": solved,
        "state": browser.state,
        "drag_distance": to_x - from_x,
        "from_x": from_x,
        "from_y": from_y,
        "to_x": to_x,
        "snapshot": browser.snapshot(),
    }


@douyin_router.post("/auto_solve_slider")
async def auto_solve_douyin_slider(request: Request):
    browser = request.app.state.douyin_browser
    if browser.lock.locked():
        raise HTTPException(409, "Trình duyệt đang được sử dụng bởi tìm kiếm.")
    try:
        page = browser.get_active_page()
    except Exception:
        raise HTTPException(404, "Trình duyệt chưa mở.")

    return await execute_slider_solve(page, browser)


@router.post("/auto_solve_slider")
async def auto_solve_xhs_slider(request: Request):
    browser = request.app.state.browser
    if browser.lock.locked():
        raise HTTPException(409, "Trình duyệt đang được sử dụng bởi tìm kiếm.")
    try:
        page = browser.get_active_page()
    except Exception:
        raise HTTPException(404, "Trình duyệt chưa mở.")

    return await execute_slider_solve(page, browser)



