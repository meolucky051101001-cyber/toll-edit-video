#!/usr/bin/env python3
"""FFmpeg Utility: Ensures local portable FFmpeg is present in bin/ or system PATH."""

import argparse
import os
import shutil
import sys
import urllib.request
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BIN_DIR = ROOT / "bin"
FFMPEG_WIN_URL = "https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip"


def find_ffmpeg() -> str | None:
    local_name = "ffmpeg.exe" if os.name == "nt" else "ffmpeg"
    local_bin = BIN_DIR / local_name
    if local_bin.is_file() and os.access(local_bin, os.X_OK | os.R_OK):
        return str(local_bin)
    return shutil.which("ffmpeg")


def download_portable_ffmpeg() -> bool:
    if os.name != "nt":
        print("Automatic download is currently configured for Windows (ffmpeg.exe). On Linux/macOS, use package manager.")
        return False
    BIN_DIR.mkdir(parents=True, exist_ok=True)
    target_exe = BIN_DIR / "ffmpeg.exe"
    target_probe = BIN_DIR / "ffprobe.exe"
    if target_exe.is_file() and target_probe.is_file():
        print(f"Portable FFmpeg already present at {target_exe}")
        return True

    zip_path = BIN_DIR / "ffmpeg-temp.zip"
    print(f"Downloading portable FFmpeg to {BIN_DIR}...")
    try:
        urllib.request.urlretrieve(FFMPEG_WIN_URL, zip_path)
        print("Extracting ffmpeg.exe and ffprobe.exe...")
        with zipfile.ZipFile(zip_path, "r") as z:
            for item in z.namelist():
                if item.endswith("ffmpeg.exe"):
                    with z.open(item) as src, open(target_exe, "wb") as dst:
                        shutil.copyfileobj(src, dst)
                elif item.endswith("ffprobe.exe"):
                    with z.open(item) as src, open(target_probe, "wb") as dst:
                        shutil.copyfileobj(src, dst)
        if zip_path.is_file():
            zip_path.unlink()
        print(f"Successfully installed portable FFmpeg to {BIN_DIR}")
        return True
    except Exception as exc:
        print(f"Failed to download FFmpeg: {exc}")
        if zip_path.is_file():
            zip_path.unlink()
        return False


def main() -> int:
    parser = argparse.ArgumentParser(description="Ensure FFmpeg binary availability.")
    parser.add_argument("--download", action="store_true", help="Download portable FFmpeg if missing")
    parser.add_argument("--check-only", action="store_true", help="Check availability without downloading")
    args = parser.parse_args()

    current = find_ffmpeg()
    if current:
        print(f"FFmpeg is available at: {current}")
        return 0

    print("FFmpeg was not found in local bin/ or system PATH.")
    if args.check_only:
        return 1

    if args.download or sys.stdin.isatty():
        ok = download_portable_ffmpeg()
        return 0 if ok else 1
    return 1


if __name__ == "__main__":
    sys.exit(main())
