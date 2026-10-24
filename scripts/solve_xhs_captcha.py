import asyncio
import sys
from pathlib import Path

# Force UTF-8 encoding for Windows console (cp1252 would crash on CJK output)
if hasattr(sys.stdout, "reconfigure"):
    getattr(sys.stdout, "reconfigure")(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    getattr(sys.stderr, "reconfigure")(encoding="utf-8", errors="replace")

ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from backend.api.settings import _save_env_values  # noqa: E402
from backend.core.config import settings  # noqa: E402
from backend.services.browser_service import (  # noqa: E402
    BrowserService,
    save_session_checkpoint,
)
from backend.services.cookie_service import extract_cookies_from_auth_file  # noqa: E402

# Use msedge to avoid single-instance mutex conflict with already open Google Chrome
settings.xhs_browser_channel = "msedge"

TOTAL_ATTEMPTS = 150  # 150 x 2s = 300 seconds to scan the QR / solve the slider


async def main():
    profile = ROOT_DIR / "data" / "browser" / "xiaohongshu"
    print("=" * 60)
    print("MỞ TRÌNH DUYỆT XIAOHONGSHU ĐỂ GIẢI CAPTCHA / ĐĂNG NHẬP")
    print("=" * 60)
    print(f"Hồ sơ trình duyệt: {profile}")
    print("Đang khởi động cửa sổ Chromium (headful)...")

    service = BrowserService(profile=profile, headless=False)
    # Force sticky headful so it opens visibly
    service.sticky_headful = True
    service.hybrid_headless = False

    try:
        # Navigate to explore feed or search
        target_url = "https://www.xiaohongshu.com/explore"
        page = await service.ensure_open(headless_override=False, initial_url=target_url)
        await page.bring_to_front()

        print("\nCửa sổ Xiaohongshu đã hiện lên màn hình của bạn!")
        print(">> Vui lòng dùng chuột kéo thanh trượt / giải CAPTCHA hoặc đăng nhập trên cửa sổ trình duyệt.")
        print(">> Script đang tự động theo dõi trạng thái...\n")

        # Poll every 2 seconds until verification is cleared
        for attempt in range(TOTAL_ATTEMPTS):
            await asyncio.sleep(2)
            if page.is_closed():
                print("Cửa sổ trình duyệt đã được đóng.")
                break

            state = await service.inspect(page)
            diag = service.diagnostics
            captcha_vis = diag.get("captcha_visible", False)
            auth = diag.get("is_authenticated", False)
            result_links = diag.get("result_links_count", 0)

            if attempt % 5 == 0:
                print(
                    f"[{attempt + 1:03d}/{TOTAL_ATTEMPTS}] Trạng thái: {state} | "
                    f"CAPTCHA: {captcha_vis} | Links: {result_links} | Đã đăng nhập: {auth}",
                    flush=True,
                )

            # If captcha is visible, attempt auto-solving a few times
            if captcha_vis and attempt in {1, 3, 7, 15}:
                print(">> Phát hiện CAPTCHA trên Xiaohongshu! Đang thử tự động kéo thanh trượt...")
                try:
                    from backend.api.browser import execute_slider_solve

                    solve_res = await execute_slider_solve(page, service)
                    if solve_res.get("ok"):
                        print(f"✓ Đã tự động giải CAPTCHA thành công! (kéo {solve_res.get('drag_distance'):.1f}px)")
                    else:
                        print(">> Tự động kéo xong, kiểm tra trạng thái tiếp theo...")
                except Exception as err:
                    print(f">> Gợi ý giải: Bạn có thể dùng chuột kéo trực tiếp trên cửa sổ trình duyệt ({err})")

            # If captcha is not visible and page is available or authenticated or has note links
            if not captcha_vis and (state in {"authenticated", "page_available"} or result_links > 0):
                print("\n" + "=" * 60)
                print("✓ XÁC MINH THÀNH CÔNG! ĐÃ VƯỢT QUA XIAOHONGSHU!")
                print("=" * 60)
                if service.context:
                    await save_session_checkpoint(service.context, profile)
                    print("Đã lưu session checkpoint vào:", profile / "auth_state.json")

                # Extract domain cookies and persist them into .env so the
                # downloader sidecar and headless search reuse the session.
                cookie_str = extract_cookies_from_auth_file(
                    profile / "auth_state.json", platform="xiaohongshu"
                )
                if cookie_str:
                    _save_env_values({"XHS_COOKIE": cookie_str})
                    settings.xhs_cookie = cookie_str
                    print("✓ Đã đồng bộ XHS_COOKIE vào file .env!")
                else:
                    print("⚠ Không trích xuất được cookie phiên từ checkpoint; hệ thống vẫn dùng auth_state.json.")

                print("Giữ cửa sổ thêm 5 giây để hoàn tất đồng bộ cookies...")
                await asyncio.sleep(5)
                break
        else:
            print("\nHết thời gian chờ (300 giây). Vui lòng thử lại khi sẵn sàng.")

    except Exception as exc:
        print(f"Lỗi: {exc}")
    finally:
        await service.close()
        print("Hoàn tất!")


if __name__ == "__main__":
    asyncio.run(main())
