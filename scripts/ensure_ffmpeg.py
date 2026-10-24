#!/usr/bin/env python3
"""FFmpeg Utility: Ensures local portable FFmpeg is present in bin/ or system PATH.

The download URL points at gyan.dev's rolling release build, so its hash
changes with every upstream update. Integrity is therefore enforced in three
ways: an optional pinned --sha256 (or FFMPEG_SHA256 env var) for controlled
distributions, a zip-validity + payload check after download, and a
post-install `ffmpeg -version` smoke test.
"""

import argparse
import hashlib
import os
import shutil
import subprocess
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


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def extract_payload(zip_path: Path, target_exe: Path, target_probe: Path) -> None:
    """Extracts ffmpeg.exe/ffprobe.exe; fails loudly when the archive is truncated."""
    with zipfile.ZipFile(zip_path, "r") as z:
        bad = z.testzip()
        if bad is not None:
            raise zipfile.BadZipFile(f"Corrupt member inside downloaded archive: {bad}")
        found_ffmpeg = False
        found_ffprobe = False
        for item in z.namelist():
            if item.endswith("ffmpeg.exe"):
                with z.open(item) as src, open(target_exe, "wb") as dst:
                    shutil.copyfileobj(src, dst)
                found_ffmpeg = True
            elif item.endswith("ffprobe.exe"):
                with z.open(item) as src, open(target_probe, "wb") as dst:
                    shutil.copyfileobj(src, dst)
                found_ffprobe = True
        if not found_ffmpeg or not found_ffprobe:
            raise zipfile.BadZipFile(
                "Downloaded archive does not contain ffmpeg.exe/ffprobe.exe; refusing to install."
            )


def verify_downloaded_binary(target_exe: Path) -> bool:
    try:
        result = subprocess.run(
            [str(target_exe), "-version"], capture_output=True, timeout=20
        )
        return result.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def download_portable_ffmpeg(expected_sha256: str | None = None) -> bool:
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
        if expected_sha256:
            actual = sha256_of(zip_path)
            if actual.lower() != expected_sha256.lower():
                zip_path.unlink(missing_ok=True)
                print(
                    f"Checksum mismatch: expected {expected_sha256}, got {actual}. "
                    "Download rejected — provide the hash of the current gyan.dev release."
                )
                return False
            print(f"Checksum OK ({actual[:16]}…)")
        print("Extracting ffmpeg.exe and ffprobe.exe...")
        extract_payload(zip_path, target_exe, target_probe)
        if zip_path.is_file():
            zip_path.unlink()
        if not verify_downloaded_binary(target_exe):
            print("Installed ffmpeg.exe does not run correctly; removing it.")
            target_exe.unlink(missing_ok=True)
            target_probe.unlink(missing_ok=True)
            return False
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
    parser.add_argument(
        "--sha256",
        default=os.environ.get("FFMPEG_SHA256") or None,
        help="Expected SHA-256 of the downloaded zip; refuses to install on mismatch",
    )
    parser.add_argument("--yes", action="store_true", help="Assume yes; download without prompting")
    args = parser.parse_args()

    current = find_ffmpeg()
    if current:
        print(f"FFmpeg is available at: {current}")
        return 0

    print("FFmpeg was not found in local bin/ or system PATH.")
    if args.check_only:
        return 1

    if args.download or args.yes:
        ok = download_portable_ffmpeg(args.sha256)
        return 0 if ok else 1
    if sys.stdin.isatty():
        answer = input(f"Download portable FFmpeg from {FFMPEG_WIN_URL} now? [y/N] ").strip().lower()
        if answer in {"y", "yes"}:
            ok = download_portable_ffmpeg(args.sha256)
            return 0 if ok else 1
        print("Skipped FFmpeg download by user choice.")
        return 1
    return 1


if __name__ == "__main__":
    sys.exit(main())
