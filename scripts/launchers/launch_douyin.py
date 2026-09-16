import os
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
DOUYIN_DIR = PROJECT_ROOT / "scratch" / "Douyin_TikTok_Download_API"

if not DOUYIN_DIR.exists():
    sys.exit(f"Douyin_TikTok_Download_API directory not found at {DOUYIN_DIR}")

sys.path.insert(0, str(DOUYIN_DIR))
os.chdir(str(DOUYIN_DIR))
sys.argv[0] = str(DOUYIN_DIR / "main.py")

try:
    import uvicorn
    from app.main import app
except ImportError as e:
    sys.exit(f"Failed to import Douyin downloader application: {e}")

if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=5555, log_level="info")
