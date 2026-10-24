import asyncio
import os
import sys
from pathlib import Path

# Force UTF-8 encoding for Windows console
os.environ["PYTHONIOENCODING"] = "utf-8"
if hasattr(sys.stdout, "reconfigure"):
    getattr(sys.stdout, "reconfigure")(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    getattr(sys.stderr, "reconfigure")(encoding="utf-8", errors="replace")

ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from backend.api.settings import _save_env_values, sync_douyin_upstream_cookie  # noqa: E402
from backend.core.config import settings  # noqa: E402
from backend.services.cookie_service import extract_cookies_from_auth_file  # noqa: E402
from backend.services.douyin_browser_service import (  # noqa: E402
    DouyinBrowserService,
    save_session_checkpoint,
)

# Use msedge to avoid single-instance mutex conflict with already open Google Chrome
settings.xhs_browser_channel = "msedge"


async def main():
    profile = ROOT_DIR / "data" / "browser" / "douyin"
    print("=" * 70)
    print("  ĐĂNG NHẬP DOUYIN & LƯU PHIÊN BỀN VỮNG (GIẢI PHÁP 1)")
    print("======================================================================")
    print(f"Hồ sơ trình duyệt: {profile}")
    print("Đang bật cửa sổ trình duyệt Douyin (Microsoft Edge / Chromium)...")

    service = DouyinBrowserService(profile=profile, headless=False)
    service.sticky_headful = True
    service.hybrid_headless = False
    service.dismiss_guest_modal = False  # Keep login modal open for QR code scanning

    try:
        home_url = "https://www.douyin.com/"
        page = await service.ensure_open(headless_override=False, initial_url=home_url)
        await page.bring_to_front()

        # Allow initial rendering
        await page.wait_for_timeout(2000)

        # Inspect current state
        state = await service.inspect(page)
        diag = service.diagnostics

        # If not authenticated, ensure the QR login modal is opened
        if not diag.get("is_authenticated", False):
            try:
                login_btn = page.locator(
                    "[data-e2e='login-button']:visible, .header-login-btn:visible, button:has-text('登录'):visible, a:has-text('登录'):visible"
                ).first
                if await login_btn.count() > 0:
                    await login_btn.click(timeout=1500)
                    await page.wait_for_timeout(1000)
            except Exception:
                pass

        print("\n" + "=" * 70)
        print(">>> CỬA SỔ DOUYIN ĐÃ HIỂN THỊ TRÊN MÀN HÌNH CỦA BẠN! <<<")
        print("=" * 70)
        print("HƯỚNG DẪN QUÉT MÃ ĐĂNG NHẬP:")
        print("  1. Mở ứng dụng Douyin (抖音) trên điện thoại của bạn.")
        print("  2. Bấm vào biểu tượng Quét mã QR (hoặc vào mục Tìm kiếm -> icon Scan).")
        print("  3. Hướng điện thoại quét mã QR đang hiển thị trên màn hình máy tính.")
        print("  4. Bấm 'Xác nhận đăng nhập' (确认登录) trên điện thoại.")
        print("  (Hoặc bạn có thể chọn tab đăng nhập bằng SĐT + mã SMS trực tiếp trên cửa sổ).")
        print("\nLƯU Ý NẾU XUẤT HIỆN CAPTCHA MẢNH GHÉP:")
        print("  - Hãy dùng chuột kéo mảnh ghép khớp vị trí trực tiếp trên cửa sổ trình duyệt.")
        print("\nHệ thống đang tự động theo dõi và lưu phiên ngay khi đăng nhập thành công...")
        print("-" * 70 + "\n")

        screenshot_path = profile / "last_captcha_screen.png"

        for attempt in range(150):  # 300 seconds
            await asyncio.sleep(2)
            if page.is_closed():
                print("Cửa sổ trình duyệt đã được đóng bởi người dùng.")
                break

            state = await service.inspect(page)
            diag = service.diagnostics
            captcha_vis = diag.get("captcha_visible", False)
            auth = diag.get("is_authenticated", False)

            user_logged_in = auth

            # If not logged in and not captcha and login modal is not visible, ensure login button is clicked
            if not user_logged_in and not captcha_vis and not diag.get("login_modal_visible", False) and attempt % 10 == 0:
                try:
                    login_btn = page.locator(
                        "[data-e2e='login-button']:visible, .header-login-btn:visible, button:has-text('登录'):visible, a:has-text('登录'):visible"
                    ).first
                    if await login_btn.count() > 0:
                        await login_btn.click(timeout=1000)
                except Exception:
                    pass

            if attempt % 5 == 0:
                print(
                    f"[{attempt + 1:03d}/150] Trạng thái: {state} | "
                    f"CAPTCHA: {captcha_vis} | Đã đăng nhập: {user_logged_in}",
                    flush=True,
                )

            if captcha_vis:
                print(">> Phát hiện mảnh ghép bảo mật. Vui lòng dùng chuột kéo thanh trượt trên cửa sổ trình duyệt.", flush=True)
                try:
                    await page.screenshot(path=str(screenshot_path))
                except Exception:
                    pass

            if user_logged_in and not captcha_vis:
                print("\n" + "=" * 70)
                print("✓ ĐĂNG NHẬP THÀNH CÔNG! ĐÃ XÁC THỰC TÀI KHOẢN DOUYIN!")
                print("=" * 70)

                # 1. Save session checkpoint atomically to profile
                if service.context:
                    await save_session_checkpoint(service.context, profile)
                    print(f"✓ Đã lưu session checkpoint vào: {profile / 'auth_state.json'}")

                # 2. Extract domain cookies and update .env
                cookie_str = extract_cookies_from_auth_file(profile / "auth_state.json", platform="douyin")
                if cookie_str:
                    _save_env_values({"DOUYIN_COOKIE": cookie_str})
                    settings.douyin_cookie = cookie_str
                    print("✓ Đã đồng bộ DOUYIN_COOKIE vào file .env!")

                    # Push to upstream downloader if configured
                    if settings.douyin_downloader_url:
                        try:
                            await sync_douyin_upstream_cookie(cookie_str, settings.douyin_downloader_url)
                            print("✓ Đã đồng bộ cookie lên upstream downloader.")
                        except Exception:
                            pass

                print("\n✓ HOÀN TẤT THIẾT LẬP GIẢI PHÁP 1!")
                print("Từ bây giờ, hệ thống sẽ tự động sử dụng phiên đăng nhập này để tìm kiếm")
                print("và cào dữ liệu Douyin ẩn danh (headless) mà không bị Douyin chặn CAPTCHA.")
                print("Đang đóng trình duyệt an toàn sau 4 giây...")
                await asyncio.sleep(4)
                break
        else:
            print("\nHết thời gian chờ (300 giây). Vui lòng thử lại khi sẵn sàng.")

    except Exception as exc:
        print(f"Lỗi: {exc}")
    finally:
        await service.close()
        print("Đã hoàn tất tiến trình.")


if __name__ == "__main__":
    asyncio.run(main())
