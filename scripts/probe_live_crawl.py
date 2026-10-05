import asyncio
import json
import sys
import time
from pathlib import Path

# Force UTF-8 output encoding for Windows PowerShell
if hasattr(sys.stdout, "reconfigure"):
    getattr(sys.stdout, "reconfigure")(encoding="utf-8", errors="replace")

ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from backend.providers.base import ProviderUnavailableError, UserActionRequired  # noqa: E402
from backend.schemas.contracts import SearchFilters  # noqa: E402


async def probe_douyin():
    print("=== LIVE CRAWL ACCEPTANCE PROBE: DOUYIN ===")
    from backend.providers.douyin.provider import DouyinProvider
    from backend.services.douyin_browser_service import DouyinBrowserService

    profile_dir = Path("data/browser/douyin")
    auth_state_file = profile_dir / "auth_state.json"
    print(f"Profile directory: {profile_dir.resolve()}")
    print(f"Auth state exists: {auth_state_file.exists()}")
    if auth_state_file.exists():
        try:
            auth_data = json.loads(auth_state_file.read_text(encoding="utf-8"))
            cookie_count = len(auth_data.get("cookies", []))
            print(f"Saved cookies in auth_state: {cookie_count} (grade: {auth_data.get('grade')})")
        except Exception as e:
            print(f"Error reading auth state: {e}")

    browser = DouyinBrowserService(profile=profile_dir, headless=True)
    provider = DouyinProvider(browser=browser, auto_elevate=False)

    query = "decor bàn học"
    print(f"Executing search query: '{query}' with limit=5, sort_by='most_liked'...")
    start_time = time.time()
    results = []
    error_note = None

    try:
        results = await provider.search(
            query,
            limit=5,
            filters=SearchFilters(sort_by="most_liked", publish_time="all", content_type="all")
        )
    except UserActionRequired as uar:
        error_note = f"UserActionRequired ({uar.state}): {uar}"
        print(f"--> Result: CAPTCHA / Action Triggered: {error_note}")
    except ProviderUnavailableError as pue:
        error_note = f"ProviderUnavailableError: {pue}"
        print(f"--> Result: Provider Unavailable: {error_note}")
    except Exception as exc:
        error_note = f"{type(exc).__name__}: {exc}"
        print(f"--> Result: Error: {error_note}")
    finally:
        crawl_duration = round(time.time() - start_time, 2)
        try:
            await provider.close()
            await browser.close()
        except Exception:
            pass

    print("\n--- Douyin Crawl Summary ---")
    print(f"- Query thực tế: {query}")
    print(f"- Số kết quả nhận được: {len(results)}")
    print(f"- Thời gian crawl: {crawl_duration}s")
    print(f"- Lỗi CAPTCHA / login: {error_note or 'None (Thành công hoàn toàn)'}")
    if results:
        print("- Danh sách video thật xác minh:")
        for idx, r in enumerate(results, 1):
            print(f"  {idx}. ID: {r.platform_video_id} | Title: {r.title} | Likes: {r.like_count} | URL: {r.url}")

    return {
        "platform": "douyin",
        "query": query,
        "results_count": len(results),
        "duration_seconds": crawl_duration,
        "error_note": error_note,
        "samples": [
            {
                "id": r.platform_video_id,
                "title": r.title,
                "likes": r.like_count,
                "url": str(r.url),
            }
            for r in results
        ]
    }


async def main():
    report = await probe_douyin()
    out_path = Path("live_crawl_probe_result.json")
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2, ensure_ascii=False)
    print(f"\nReport written to: {out_path.resolve()}")


if __name__ == "__main__":
    asyncio.run(main())
