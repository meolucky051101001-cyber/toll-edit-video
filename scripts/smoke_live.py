#!/usr/bin/env python3
"""Live smoke test: run ONE real search on Douyin/Xiaohongshu and report PASS/FAIL.

Purpose: the offline suite mocks every platform interaction, so a Douyin/XHS
DOM or API change keeps 574 tests green while production breaks. This script
executes a single real crawl using the persisted login profile and prints an
unambiguous PASS/FAIL with the result count.

Usage:
  python scripts/smoke_live.py --platform douyin --query "unbox đồ cute" --min-results 3
  python scripts/smoke_live.py --platform both --min-results 1

Exit codes: 0 = PASS (all requested platforms reached the minimum), 1 = FAIL,
2 = usage/setup error.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import time
from pathlib import Path

# Windows consoles default to cp1252: CJK/UTF-8 output would crash the script.
if hasattr(sys.stdout, "reconfigure"):
    getattr(sys.stdout, "reconfigure")(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    getattr(sys.stderr, "reconfigure")(encoding="utf-8", errors="replace")

ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

TOTAL_TIMEOUT_SECONDS = 600  # one search per platform must fit in 10 minutes


async def smoke_platform(platform: str, query: str, min_results: int) -> tuple[bool, str]:
    """Runs one real search; returns (passed, detail)."""
    from backend.providers.base import ProviderError, UserActionRequired
    from backend.schemas.contracts import SearchFilters

    if platform == "douyin":
        from backend.providers.douyin.provider import DouyinProvider
        from backend.services.douyin_browser_service import DouyinBrowserService

        browser = DouyinBrowserService(headless=True)
        provider = DouyinProvider(browser=browser, auto_elevate=False)
    else:
        from backend.providers.xiaohongshu.provider import XiaohongshuProvider
        from backend.services.browser_service import BrowserService

        browser = BrowserService(headless=True)
        provider = XiaohongshuProvider(browser=browser, auto_elevate=False)

    try:
        started = time.perf_counter()
        results = await provider.search(query, min_results, SearchFilters())
        elapsed = time.perf_counter() - started
        detail = f"{len(results)} kết quả trong {elapsed:.1f}s (yêu cầu ≥ {min_results})"
        return len(results) >= min_results, detail
    except UserActionRequired as exc:
        return False, (
            f"Nền tảng yêu cầu đăng nhập/xác minh ({exc}). Hãy chạy giai_captcha_{platform}.bat "
            "hoặc mở trình duyệt riêng để đăng nhập lại, rồi chạy smoke test."
        )
    except ProviderError as exc:
        return False, f"Lỗi provider: {exc}"
    except Exception as exc:  # noqa: BLE001 — the smoke test must report, not crash
        return False, f"Lỗi không phân loại: {type(exc).__name__}: {exc}"
    finally:
        try:
            await browser.close()
        except Exception:
            pass


async def main_async(args: argparse.Namespace) -> int:
    platforms = ["douyin", "xiaohongshu"] if args.platform == "both" else [args.platform]
    print("=" * 64)
    print("SMOKE TEST LIVE — chạy 1 lượt tìm thật để phát hiện thay đổi DOM/API")
    print("=" * 64)

    failures: list[str] = []
    for platform in platforms:
        print(f"\n[{platform}] Tìm '{args.query}' (headless, dùng phiên đã lưu)...")
        try:
            passed, detail = await asyncio.wait_for(
                smoke_platform(platform, args.query, args.min_results),
                timeout=TOTAL_TIMEOUT_SECONDS,
            )
        except asyncio.TimeoutError:
            passed, detail = False, f"Vượt {TOTAL_TIMEOUT_SECONDS}s — nền tảng có thể đã đổi cấu trúc hoặc mạng chậm."
        status = "PASS" if passed else "FAIL"
        print(f"[{platform}] {status}: {detail}")
        if not passed:
            failures.append(platform)

    print("\n" + "=" * 64)
    if failures:
        print(f"KẾT QUẢ: FAIL trên {', '.join(failures)}. Kiểm tra logs/ và trạng thái phiên đăng nhập.")
        return 1
    print("KẾT QUẢ: PASS — nền tảng trả kết quả thật đúng kỳ vọng.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="One-shot live smoke test for Douyin/Xiaohongshu search.")
    parser.add_argument("--platform", choices=["douyin", "xiaohongshu", "both"], default="both")
    parser.add_argument("--query", default="unbox đồ cute", help="Chủ đề tiếng Việt dùng để tìm thử")
    parser.add_argument("--min-results", type=int, default=3, help="Số kết quả tối thiểu coi là PASS")
    args = parser.parse_args()
    try:
        return asyncio.run(main_async(args))
    except KeyboardInterrupt:
        print("\nĐã hủy bởi người dùng.")
        return 1


if __name__ == "__main__":
    sys.exit(main())
