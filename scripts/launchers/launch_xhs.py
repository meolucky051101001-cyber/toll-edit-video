import asyncio
import os
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
XHS_DIR = PROJECT_ROOT / "scratch" / "XHS-Downloader"

if not XHS_DIR.exists():
    sys.exit(f"XHS-Downloader directory not found at {XHS_DIR}")

sys.path.insert(0, str(XHS_DIR))
os.chdir(str(XHS_DIR))
sys.argv[0] = str(XHS_DIR / "main.py")

from source import XHS, Settings


async def main() -> None:
    async with XHS(**Settings().run()) as xhs:
        await xhs.run_api_server(host="127.0.0.1", port=5556, log_level="info")


if __name__ == "__main__":
    asyncio.run(main())
