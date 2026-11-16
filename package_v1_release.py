"""Independent packaging and validation script for Tool V1 release.

Generates a clean, deterministic release zip archive for code review / deployment,
using a shared selection policy with scripts/generate_release_manifest.py that
excludes machine learning model weights, virtual environments, caches, secrets,
and runtime data.
"""
from __future__ import annotations

import argparse
import hashlib
import os
import re
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path
from typing import Iterable, List, Set

ROOT_DIR = Path(__file__).resolve().parent
OUTPUT_ZIP = ROOT_DIR / "Tool_V1.0_Reviewed_RC3.zip"

EXCLUDE_DIRS = {
    ".git",
    ".pytest_cache",
    ".agent",
    ".vscode",
    "_review_src",
    "venv",
    "venv_py310",
    "venv_py312",
    "model_venv",
    "backend/venv",
    "backend/venv_py310",
    "backend/venv_py312",
    "backend/model_venv",
    "models",
    "backend/models",
    "backend/cloud/results",
    "MyVoiceModel_v2",
    "__pycache__",
    "temp",
    "temp_dir",
    "desktop_shortcuts_backup",
    "export_giam_sat_a2ui",
    "workspace",
    "output",
    "backups",
    "logs",
    "runtime_logs",
    "voice_cache",
    "node_modules",
    "scratch",
    "scripts_and_tests",
    "fixes_for_v1",
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
    ".mkv",
    ".avi",
    ".mov",
    ".zip",
    ".tar",
    ".gz",
    ".log",
    ".tmp",
    ".swp",
    ".swo",
    ".db",
    ".sqlite",
    ".sqlite3",
    ".pyc",
    ".pyo",
    ".pyd",
    ".patch",
    ".pem",
    ".key",
}

ALLOWED_EXTENSIONS = {
    ".py",
    ".html",
    ".css",
    ".js",
    ".cjs",
    ".mjs",
    ".ts",
    ".tsx",
    ".json",
    ".md",
    ".txt",
    ".ps1",
    ".bat",
    ".vbs",
    ".ico",
    ".png",
    ".svg",
    ".yml",
    ".yaml",
    ".ini",
    ".lock",
    ".sha256",
    ".example",
}
CLOUD_CAPCUT_WHEEL = "backend/cloud/gpu_scripts/capcut_tts_api-1.0.0-py3-none-any.whl"
CLOUD_RELEASE_FILES = (
    CLOUD_CAPCUT_WHEEL,
    "backend/cloud/gpu_scripts/cloud_env_manifest.json",
    "run_cloud_node.py",
    "start_cloud_dashboard.bat",
)

EXCLUDE_FILES = {
    "Tool_V1.0_Final_RC2.zip",
    "Tool_V1.0_Reviewed_RC3.zip",
    "HE_THONG_GIAM_SAT_VA_A2UI.zip",
    "app.log",
    "telebot_downloader.log",
    "RELEASE_MANIFEST.sha256",
    "RELEASE_METADATA.json",
    "desktop.ini",
    "Thumbs.db",
    "backend/ai/_kinetic_v3ref.py",
    "backend/check_today_batch.py",
    "backend/check_voice_catalog.py",
    "backend/find_all_links.py",
    "backend/test_links.py",
    "backend/test_segment_editor.py",
    "backend/test_verify_codex.py",
    "backend/test_voice_overlap_fix.py",
    "backend/test_voice_preview.py",
    "backend/update_dashboards_phase1.py",
}

ALLOWED_TOP_DIRS = ["backend", "tests", "docs", "assets", "scripts", "tools", ".github"]
ALLOWED_TOP_FILES = [
    ".env.cpu.example",
    ".env.gpu.example",
    "BACKUP_TOOL_V1.bat",
    "conftest.py",
    "DASHBOARD_MONITOR_HANDOVER.md",
    "DOCS_HE_THONG_GIAM_SAT_VA_A2UI.md",
    "download_audio.py",
    "extract_wav.py",
    "GIAI_MA_SHEET.bat",
    "Giao_Dien_Quan_Sat_Tool.bat",
    "HUONG_DAN_BOT_TAI_VIDEO.md",
    "KhoiDongAn.vbs",
    "MA_HOA_SHEET.bat",
    "MoDieuKhien.vbs",
    "package_v1_release.py",
    "pytest.ini",
    "README.md",
    "README_FIRST.md",
    "requirements-dev.txt",
    "requirements-models.txt",
    "requirements.lock",
    "requirements.lock.txt",
    "requirements.txt",
    "rollback_to_safe_state.ps1",
    "run_batch_edit.bat",
    "run_cloud_node.py",
    "setup_new_laptop.ps1",
    "setup_v1.ps1",
    "start_bot.bat",
    "start_cloud_dashboard.bat",
    "start_telebot_downloader.bat",
    "stop_bot.bat",
    "stop_telebot_downloader.bat",
    "telebot_video_downloader.py",
    "TOOL_V2_FULL_GUIDE.md",
    "urls.example.txt",
    "V1_BASELINE_MANIFEST.sha256",
    "VERSIONS.md",
]

REQUIRED_RELEASE_FILES = [
    "backend/main.py",
    "backend/job_tracker.py",
    "backend/v1_shutdown.py",
    "backend/service_control.py",
    "backend/ai/pyav_compat.py",
    "backend/templates/dashboard.html",
    "scripts/verify_runtime_health.py",
    "scripts/cutover_py312.bat",
    "scripts/rollback_py310.bat",
    "scripts/generate_release_manifest.py",
    "tools/render_node/package.json",
    "tools/render_node/package-lock.json",
    "requirements.txt",
    "requirements.lock",
    "tests/test_v1_phase9_1_final_gate.py",
    "README.md",
    "README_FIRST.md",
    "start_bot.bat",
    "stop_bot.bat",
    "scripts/windows/TatHoanToan.bat",
]


def should_include(rel_path: str | Path) -> bool:
    normalized = str(rel_path).replace("\\", "/").strip("/")
    if not normalized:
        return False
    parts = normalized.split("/")
    lower_parts = [part.lower() for part in parts]

    if ".." in parts or str(rel_path).startswith(("/", "\\")) or ":" in normalized:
        return False

    # Reject .env files except explicit .example templates
    for part in lower_parts:
        if part.startswith(".env") and not part.endswith(".example"):
            return False

    filename_lower = lower_parts[-1]
    if any(
        word in filename_lower
        for word in ("token", "credential", "cookie", "service_account", "oauth_client", "secret")
    ):
        # Allow legitimate code/test modules that test token/cookie handling but never data/json/txt/env files
        if not filename_lower.endswith((".py", ".ps1", ".bat", ".md")):
            return False
        if filename_lower in {"export_douyin_cookies.py"}:
            pass
        elif any(w in filename_lower for w in ("gdrive_token", "oauth_client", "service_account")):
            return False

    if "before-" in filename_lower or ".before." in filename_lower or ".before-" in filename_lower:
        return False

    if any(
        part in {
            "venv",
            "venv_py310",
            "venv_py312",
            "model_venv",
            "voice_cache",
            "runtime_logs",
            "node_modules",
            "scratch",
            "scripts_and_tests",
            "fixes_for_v1",
            "__pycache__",
            ".git",
            ".pytest_cache",
            ".agent",
        }
        or part.startswith("venv")
        for part in lower_parts
    ):
        return False

    ext = Path(normalized).suffix.lower()
    if normalized != CLOUD_CAPCUT_WHEEL and (ext not in ALLOWED_EXTENSIONS or ext in EXCLUDE_EXTENSIONS):
        return False

    if normalized in EXCLUDE_FILES or parts[-1] in EXCLUDE_FILES:
        return False

    for d in EXCLUDE_DIRS:
        if normalized == d or normalized.startswith(f"{d}/") or any(p == d for p in parts):
            return False

    if len(parts) > 1:
        top_dir = parts[0]
        if top_dir not in ALLOWED_TOP_DIRS:
            return False
    else:
        if parts[0] not in ALLOWED_TOP_FILES:
            return False

    return True


def _git_tracked_files(root_dir: Path) -> Set[str] | None:
    git_dir = root_dir / ".git"
    if not git_dir.exists():
        return None
    try:
        proc = subprocess.run(
            ["git", "-c", f"safe.directory={root_dir.resolve().as_posix()}", "-C", str(root_dir), "ls-files", "-z"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=15,
            check=False,
        )
        if proc.returncode != 0:
            raise RuntimeError("Cannot read Git release inventory; refusing filesystem fallback")
        return {entry.replace("\\", "/") for entry in proc.stdout.split("\0") if entry}
    except (OSError, subprocess.SubprocessError) as error:
        raise RuntimeError("Cannot read Git release inventory; refusing filesystem fallback") from error


def iter_release_files(root_dir: Path = ROOT_DIR) -> List[Path]:
    """Return sorted relative Paths of files eligible for release packaging & manifest."""
    root_resolved = root_dir.resolve()
    tracked = _git_tracked_files(root_dir)
    selected: List[Path] = []

    if tracked is not None:
        candidates = sorted(tracked)
        for rel_str in candidates:
            if not should_include(rel_str):
                continue
            full_path = root_dir / rel_str
            if not full_path.is_file():
                continue
            if full_path.is_symlink() or not full_path.resolve().is_relative_to(root_resolved):
                raise RuntimeError(f"External link or path traversal not allowed: {rel_str}")
            selected.append(Path(rel_str.replace("/", os.sep)))
        return sorted(selected, key=lambda p: str(p).replace("\\", "/"))

    for current_root, dirs, files in os.walk(root_dir):
        rel_root = Path(current_root).relative_to(root_dir)
        dirs[:] = [
            d
            for d in sorted(dirs)
            if (d == ".github" or not d.startswith("."))
            and not any(x in d.lower() for x in ("venv", "cache", "node_modules", "scratch"))
            and d not in EXCLUDE_DIRS
            and (rel_root != Path(".") or d in ALLOWED_TOP_DIRS)
        ]
        for file in sorted(files):
            full_path = Path(current_root) / file
            rel_path = full_path.relative_to(root_dir)
            rel_str = str(rel_path).replace("\\", "/")
            if not should_include(rel_str):
                continue
            if full_path.is_symlink() or not full_path.resolve().is_relative_to(root_resolved):
                raise RuntimeError(f"External link or path traversal not allowed: {rel_str}")
            selected.append(rel_path)

    return sorted(selected, key=lambda p: str(p).replace("\\", "/"))


def _load_configured_secrets(root_dir: Path) -> List[bytes]:
    configured_secrets: List[bytes] = []
    env_file = root_dir / "backend" / ".env"
    if env_file.is_file():
        for line in env_file.read_text(encoding="utf-8-sig", errors="replace").splitlines():
            match = re.match(r"\s*([A-Z0-9_]*(?:KEY|TOKEN|SECRET|PASSWORD)[A-Z0-9_]*)\s*=\s*(.+)", line)
            if match:
                value = match[2].strip().strip("'\"")
                if len(value) >= 12 and not any(
                    marker in value.lower() for marker in ("your_", "example", "placeholder", "dummy")
                ):
                    configured_secrets.append(value.encode("utf-8"))
    return configured_secrets


def verify_required_files(
    rel_paths: Iterable[str],
    required_files: Iterable[str] = REQUIRED_RELEASE_FILES,
) -> None:
    normalized_set = {str(p).replace("\\", "/") for p in rel_paths}
    cloud_files = CLOUD_RELEASE_FILES if "backend/cloud/cloud_config.py" in normalized_set else ()
    for req in (*required_files, *cloud_files):
        if req not in normalized_set:
            raise RuntimeError(f"Missing required file in release: {req}")


SENSITIVE_PATTERNS = (
    re.compile(r"""(?:bot_token|api_key|secret|password)\s*=\s*['\"][A-Za-z0-9_\-:]{20,}['\"]""", re.IGNORECASE),
    re.compile(r"AIzaSy[A-Za-z0-9_\-]{33}"),
    re.compile(r"[0-9]{8,11}:[A-Za-z0-9_\-]{34,36}"),
)


def check_secrets_hygiene(root: Path, files: Iterable[Path]) -> list[tuple[Path, str]]:
    """One validator for ZIP and manifest. Never report credential values."""
    configured = _load_configured_secrets(root)
    violations = []
    for rel in files:
        try:
            raw = (root / rel).read_bytes()
        except OSError as error:
            raise RuntimeError(f"Cannot inspect release file: {rel}") from error
        if any(secret in raw for secret in configured):
            violations.append((rel, "Configured credential copy"))
            continue
        content = raw.decode("utf-8", errors="ignore")
        for pattern in SENSITIVE_PATTERNS:
            match = pattern.search(content)
            if not match:
                continue
            if "test" in str(rel).lower() and any(marker in match[0].lower() for marker in (
                "dummy", "mock", "example", "test", "synthetic", "secret_", "fake",
            )):
                continue
            violations.append((rel, "Credential literal pattern"))
    return violations


def create_release_zip(
    root_dir: Path = ROOT_DIR,
    output_zip: Path = OUTPUT_ZIP,
    dry_run: bool = False,
    required_files: Iterable[str] = REQUIRED_RELEASE_FILES,
):
    print("[*] Starting clean packaging for Tool V1...")
    if not dry_run and output_zip.exists():
        raise FileExistsError(f"Refusing to overwrite existing release: {output_zip}")

    files = iter_release_files(root_dir)
    violations = check_secrets_hygiene(root_dir, files)
    if violations:
        raise RuntimeError("Credential validation failed for release files: " + ", ".join(str(path) for path, _ in violations))

    total_uncompressed_bytes = 0
    normalized_names: List[str] = []
    for rel_path in files:
        full_path = root_dir / rel_path
        rel_str = str(rel_path).replace("\\", "/")
        raw_bytes = full_path.read_bytes()
        total_uncompressed_bytes += len(raw_bytes)
        normalized_names.append(rel_str)

    verify_required_files(normalized_names, required_files=required_files)

    if dry_run:
        print(f"[+] Dry-run complete: {len(normalized_names)} files selected ({total_uncompressed_bytes / (1024 * 1024):.2f} MB uncompressed).")
        return {
            "dry_run": True,
            "file_count": len(normalized_names),
            "uncompressed_bytes": total_uncompressed_bytes,
            "files": normalized_names,
        }

    output_zip.parent.mkdir(parents=True, exist_ok=True)
    fd, staging_name = tempfile.mkstemp(prefix=f".{output_zip.stem}_", suffix=".zip.tmp", dir=str(output_zip.parent))
    os.close(fd)
    staging_path = Path(staging_name)

    try:
        with zipfile.ZipFile(staging_path, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as zipf:
            for rel_path in files:
                full_path = root_dir / rel_path
                rel_str = str(rel_path).replace("\\", "/")
                zipf.write(full_path, arcname=rel_str)

        print("[*] Verifying archive integrity in staging before finalizing...")
        with zipfile.ZipFile(staging_path, "r") as check_zip:
            names = check_zip.namelist()
            verify_required_files(names, required_files=required_files)
            for n in names:
                if not should_include(n):
                    raise RuntimeError(f"Forbidden release entry: {n}")
                nl = n.lower()
                if any(nl.endswith(ext) for ext in [".pth", ".pt", ".bin", ".onnx", ".mp4", ".wav"]):
                    raise RuntimeError(f"Forbidden binary model/media found in zip: {n}")
                if "venv/" in nl or ".git/" in nl or "node_modules/" in nl:
                    raise RuntimeError(f"Forbidden venv, git, or node_modules file found in zip: {n}")

        staging_path.replace(output_zip)
    except Exception:
        staging_path.unlink(missing_ok=True)
        raise

    zip_size = output_zip.stat().st_size
    zip_size_mb = zip_size / (1024 * 1024)

    hasher = hashlib.sha256()
    with open(output_zip, "rb") as f:
        while chunk := f.read(65536):
            hasher.update(chunk)
    sha256_hash = hasher.hexdigest()

    print("[+] Packaging complete!")
    print(f"    - Output: {output_zip.name}")
    print(f"    - Files included: {len(files)}")
    print(f"    - Uncompressed size: {total_uncompressed_bytes / (1024 * 1024):.2f} MB")
    print(f"    - Zip archive size: {zip_size_mb:.2f} MB ({zip_size} bytes)")
    print(f"    - SHA-256: {sha256_hash}")
    print("[+] Archive verification PASSED! Clean and ready for review.")
    return sha256_hash, zip_size_mb


def main(argv: List[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Package Tool V1 release archive safely.")
    parser.add_argument("--root", type=Path, default=ROOT_DIR, help="Source repository root")
    parser.add_argument("--output", type=Path, default=OUTPUT_ZIP, help="Output ZIP file path")
    parser.add_argument("--dry-run", action="store_true", help="List and validate release files without writing ZIP")
    args = parser.parse_args(argv)
    create_release_zip(root_dir=args.root, output_zip=args.output, dry_run=args.dry_run)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
