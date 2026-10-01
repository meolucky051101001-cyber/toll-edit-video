"""Independent packaging and validation script for Tool V1.0 Final release.

Generates a clean, lightweight release zip archive for code review / deployment,
excluding machine learning model weights, virtual environments, caches, and runtime data.
"""
import os
import sys
import zipfile
import hashlib
import tempfile
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent
OUTPUT_ZIP = ROOT_DIR / "Tool_V1.0_Reviewed_RC3.zip"

EXCLUDE_DIRS = {
    ".git",
    ".pytest_cache",
    ".agent",
    "_review_src",
    "venv",
    "backend/venv",
    "models",
    "backend/models",
    "MyVoiceModel_v2",
    "__pycache__",
    "temp_dir",
    "desktop_shortcuts_backup",
    "export_giam_sat_a2ui",
    "workspace",
}

EXCLUDE_EXTENSIONS = {
    ".pth",
    ".pt",
    ".bin",
    ".onnx",
    ".safetensors",
    ".mp4",
    ".wav",
    ".mp3",
    ".zip",
    ".log",
    ".tmp",
    ".pyc",
    ".pyo",
    ".patch",
    ".pem",
    ".key",
}

EXCLUDE_FILES = {
    "Tool_V1.0_Final_RC2.zip",
    "HE_THONG_GIAM_SAT_VA_A2UI.zip",
    "app.log",
}

ALLOWED_TOP_DIRS = ["backend", "tests", "docs", "assets"]
ALLOWED_TOP_FILES = [
    "README.md",
    "README_FIRST.md",
    "CHATGPT_REVIEW_GUIDE.md",
    "TOOL_V2_FULL_GUIDE.md",
    "DASHBOARD_MONITOR_HANDOVER.md",
    "DOCS_HE_THONG_GIAM_SAT_VA_A2UI.md",
    "requirements.txt",
    "setup_v1.ps1",
    "setup_new_laptop.ps1",
    "start_bot.bat",
    "stop_bot.bat",
    "Giao_Dien_Quan_Sat_Tool.bat",
    "RESTORE_SAFE_BASELINE.bat",
    "KhoiDongAn.vbs",
    "MoDieuKhien.vbs",
    "TatHoanToan.bat",
    "run_batch_edit.bat",
    "download_audio.py",
    "extract_wav.py",
    "v1_code_changes_only.patch",
    "v1_master_fix_all_phases.patch",
    "package_v1_release.py",
]


def should_include(rel_path: str) -> bool:
    normalized = rel_path.replace("\\", "/")
    parts = normalized.split("/")
    lower_parts = [part.lower() for part in parts]
    if ".." in parts or normalized.startswith("/") or ":" in normalized:
        return False
    if any(part.startswith(".env") and part != ".env.example" for part in lower_parts):
        return False
    if any(word in lower_parts[-1] for word in ("token", "credential", "cookie", "service_account", "oauth_client")):
        return False
    if any(part.lower() in {"model_venv", "voice_cache", "runtime_logs"} for part in parts):
        return False
    if Path(normalized).suffix.lower() not in {".py", ".html", ".css", ".js", ".cjs", ".md", ".txt", ".ps1", ".bat", ".vbs", ".ico", ".png", ".example"}:
        return False

    # Check exclude extensions
    ext = os.path.splitext(normalized)[1].lower()
    if ext in EXCLUDE_EXTENSIONS:
        return False

    # Check exclude specific files
    if normalized in EXCLUDE_FILES or parts[-1] in EXCLUDE_FILES:
        return False

    # Check exclude directory parts
    for d in EXCLUDE_DIRS:
        if d in normalized or any(p == d for p in parts):
            return False

    # Top-level directory filtering
    if len(parts) > 1:
        top_dir = parts[0]
        if top_dir not in ALLOWED_TOP_DIRS:
            return False
    else:
        # Top-level file
        if parts[0] not in ALLOWED_TOP_FILES:
            return False

    return True


def create_release_zip():
    print(f"[*] Starting clean packaging for Tool V1.0 Final...")
    if OUTPUT_ZIP.exists():
        raise FileExistsError(f"Refusing to overwrite existing release: {OUTPUT_ZIP}")

    included_count = 0
    total_uncompressed_bytes = 0
    # Prevent copies of configured credentials inside source/docs from leaking too.
    import re
    configured_secrets = []
    env_file = ROOT_DIR / "backend" / ".env"
    if env_file.is_file():
        for line in env_file.read_text(encoding="utf-8-sig").splitlines():
            match = re.match(r"\s*([A-Z0-9_]*(?:KEY|TOKEN|SECRET|PASSWORD)[A-Z0-9_]*)\s*=\s*(.+)", line)
            if match:
                value = match[2].strip().strip("'\"")
                if len(value) >= 12 and not any(marker in value.lower() for marker in ("your_", "example", "placeholder")):
                    configured_secrets.append(value.encode("utf-8"))

    with zipfile.ZipFile(OUTPUT_ZIP, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as zipf:
        for root, dirs, files in os.walk(ROOT_DIR):
            # Prune before descending, rather than walking gigabytes of excluded data.
            dirs[:] = [d for d in dirs
                       if not d.startswith(".")
                       and not any(x in d.lower() for x in ("venv", "cache"))
                       and d not in EXCLUDE_DIRS
                       and (Path(root) != ROOT_DIR or d in ALLOWED_TOP_DIRS)]
            for file in files:
                full_path = Path(root) / file
                rel_path = full_path.relative_to(ROOT_DIR)
                rel_str = str(rel_path)

                if should_include(rel_str):
                    if full_path.is_symlink() or full_path.resolve().is_relative_to(ROOT_DIR.resolve()) is False:
                        raise RuntimeError(f"External link not allowed: {rel_str}")
                    if any(secret in full_path.read_bytes() for secret in configured_secrets):
                        raise RuntimeError(f"Credential copy detected; refusing release: {rel_str}")
                    zipf.write(full_path, arcname=rel_str)
                    included_count += 1
                    total_uncompressed_bytes += full_path.stat().st_size

    zip_size = OUTPUT_ZIP.stat().st_size
    zip_size_mb = zip_size / (1024 * 1024)

    # Compute SHA256
    hasher = hashlib.sha256()
    with open(OUTPUT_ZIP, "rb") as f:
        while chunk := f.read(65536):
            hasher.update(chunk)
    sha256_hash = hasher.hexdigest()

    print(f"[+] Packaging complete!")
    print(f"    - Output: {OUTPUT_ZIP.name}")
    print(f"    - Files included: {included_count}")
    print(f"    - Uncompressed size: {total_uncompressed_bytes / (1024 * 1024):.2f} MB")
    print(f"    - Zip archive size: {zip_size_mb:.2f} MB ({zip_size} bytes)")
    print(f"    - SHA-256: {sha256_hash}")

    # Integrity verification
    print(f"[*] Verifying archive integrity...")
    with zipfile.ZipFile(OUTPUT_ZIP, "r") as check_zip:
        names = check_zip.namelist()
        
        # Check required files
        required = [
            "backend/main.py",
            "backend/job_tracker.py",
            "backend/templates/dashboard.html",
            "tests/test_v1_phase9_1_final_gate.py",
            "CHATGPT_REVIEW_GUIDE.md",
            "README.md",
            "start_bot.bat",
            "stop_bot.bat",
        ]
        for req in required:
            if req not in names:
                raise RuntimeError(f"Missing required file in archive: {req}")

        # Check forbidden patterns
        for n in names:
            if not should_include(n):
                raise RuntimeError(f"Forbidden release entry: {n}")
            nl = n.lower()
            if any(nl.endswith(ext) for ext in [".pth", ".pt", ".bin", ".onnx", ".mp4", ".wav"]):
                raise RuntimeError(f"Forbidden binary model/media found in zip: {n}")
            if "venv/" in n or ".git/" in n:
                raise RuntimeError(f"Forbidden venv or git file found in zip: {n}")

    print(f"[+] Archive verification PASSED! Clean and ready for review.")
    return sha256_hash, zip_size_mb


if __name__ == "__main__":
    create_release_zip()
