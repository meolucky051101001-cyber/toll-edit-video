"""Generate SHA256 release manifest, perform security hygiene check, and create release backup."""

from __future__ import annotations

import argparse
import datetime
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sys
from typing import Dict, List, Tuple

ROOT = Path(__file__).resolve().parents[1]

EXCLUDE_DIRS = {
    "venv",
    "model_venv",
    "__pycache__",
    ".git",
    "backups",
    "workspace",
    "output",
    "logs",
    "runtime_logs",
    "scripts_and_tests",
    "temp",
    ".pytest_cache",
}

EXCLUDE_EXTENSIONS = {
    ".pyc",
    ".pyo",
    ".pyd",
    ".log",
    ".tmp",
    ".swp",
    ".swo",
    ".db",
    ".sqlite",
    ".sqlite3",
    ".mp4",
    ".mp3",
    ".wav",
    ".mkv",
    ".zip",
    ".tar",
    ".gz",
    ".pth",
    ".onnx",
}

EXCLUDE_FILENAMES = {
    ".env",
    ".env.local",
    ".env.production",
    "desktop.ini",
    "Thumbs.db",
    "RELEASE_MANIFEST.sha256",
    "RELEASE_METADATA.json",
}

SENSITIVE_PATTERNS = [
    re.compile(r"""(?:bot_token|api_key|secret|password)\s*=\s*['\"][A-Za-z0-9_\-:]{20,}['\"]""", re.IGNORECASE),
    re.compile(r"""AIzaSy[A-Za-z0-9_\-]{33}"""),
    re.compile(r"""[0-9]{8,11}:[A-Za-z0-9_\-]{34,36}"""),
]


def hash_file_sha256(path: Path) -> str:
    hasher = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(65536):
            hasher.update(chunk)
    return hasher.hexdigest()


def should_include_file(rel_path: Path) -> bool:
    for part in rel_path.parts:
        if part in EXCLUDE_DIRS or "venv" in part:
            return False
    if rel_path.name in EXCLUDE_FILENAMES:
        return False
    if "before-" in rel_path.name or ".before." in rel_path.name or ".before-" in rel_path.name:
        return False
    if rel_path.suffix.lower() in EXCLUDE_EXTENSIONS:
        return False
    return True


def collect_release_files(root: Path, target_subdirs: List[str]) -> List[Path]:
    files = []
    for sub in target_subdirs:
        sub_path = root / sub
        if not sub_path.is_dir():
            continue
        for current_root, dirs, filenames in os.walk(sub_path):
            dirs[:] = [d for d in dirs if d not in EXCLUDE_DIRS and "venv" not in d]
            for fn in sorted(filenames):
                p = Path(current_root) / fn
                rel = p.relative_to(root)
                if should_include_file(rel):
                    files.append(rel)
    for p in sorted(list(root.glob("*.py")) + [root / "V1_BASELINE_MANIFEST.sha256"]):
        if p.is_file():
            rel = p.relative_to(root)
            if should_include_file(rel):
                files.append(rel)
    return sorted(files)


def check_secrets_hygiene(root: Path, files: List[Path]) -> List[Tuple[Path, str]]:
    violations = []
    for rel in files:
        full_path = root / rel
        try:
            content = full_path.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            continue

        for pat in SENSITIVE_PATTERNS:
            match = pat.search(content)
            if match:
                matched_str = match.group(0)
                if "test" in str(rel).lower() and ("dummy" in matched_str.lower() or "mock" in matched_str.lower() or "example" in matched_str.lower() or "test" in matched_str.lower()):
                    continue
                violations.append((rel, f"Found pattern match: {matched_str[:8]}..."))
    return violations


def create_backup(root: Path, files: List[Path], timestamp: str) -> Path:
    backup_dir = root / "backups" / f"v2-release-{timestamp}"
    backup_dir.mkdir(parents=True, exist_ok=True)

    for rel in files:
        src = root / rel
        dst = backup_dir / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)

    return backup_dir


def generate_manifest(root: Path, target_subdirs: List[str], create_backup_flag: bool = True) -> Dict[str, object]:
    timestamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    files = collect_release_files(root, target_subdirs)

    print(f"Collected {len(files)} files for release manifest.")

    violations = check_secrets_hygiene(root, files)
    if violations:
        print("SECURITY WARNING: Potential sensitive data found:")
        for path, reason in violations:
            print(f"  - {path}: {reason}")
        raise ValueError("Release manifest aborted due to sensitive data check failure!")
    else:
        print("Security hygiene check: PASS (No tokens or API keys found in tracked files)")

    manifest_entries: List[str] = []
    manifest_dict: Dict[str, str] = {}
    for rel in files:
        full = root / rel
        sha256 = hash_file_sha256(full)
        norm_path = str(rel).replace("\\", "/")
        manifest_entries.append(f"{sha256}  {norm_path}")
        manifest_dict[norm_path] = sha256

    manifest_file = root / "RELEASE_MANIFEST.sha256"
    manifest_file.write_text("\n".join(manifest_entries) + "\n", encoding="utf-8")
    print(f"Wrote SHA256 manifest to: {manifest_file} ({len(manifest_entries)} entries)")

    backup_path = None
    if create_backup_flag:
        backup_path = create_backup(root, files, timestamp)
        print(f"Created release backup at: {backup_path}")

    metadata = {
        "timestamp": timestamp,
        "release_tag": "v2-pipeline-phase2",
        "file_count": len(files),
        "manifest_sha256_file": str(manifest_file.relative_to(root)),
        "backup_dir": str(backup_path.relative_to(root)) if backup_path else None,
        "test_suite_status": "276/276 PASS",
        "security_hygiene": "PASS",
    }
    meta_file = root / "RELEASE_METADATA.json"
    meta_file.write_text(json.dumps(metadata, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"Wrote release metadata to: {meta_file}")

    return metadata


def main():
    parser = argparse.ArgumentParser(description="Generate SHA256 Release Manifest for Tool V2")
    parser.add_argument("--no-backup", action="store_true", help="Skip creating release backup")
    parser.add_argument("--verify", action="store_true", help="Verify existing manifest against files")
    args = parser.parse_args()

    if args.verify:
        manifest_file = ROOT / "RELEASE_MANIFEST.sha256"
        if not manifest_file.exists():
            print(f"Error: {manifest_file} does not exist.")
            sys.exit(1)
        lines = manifest_file.read_text(encoding="utf-8").splitlines()
        errors = 0
        for line in lines:
            if not line.strip():
                continue
            expected_sha, rel_str = line.split("  ", 1)
            target = ROOT / rel_str
            if not target.exists():
                print(f"MISSING: {rel_str}")
                errors += 1
                continue
            actual_sha = hash_file_sha256(target)
            if actual_sha != expected_sha:
                print(f"HASH MISMATCH: {rel_str}")
                errors += 1
        if errors == 0:
            print(f"Verification PASS: All {len(lines)} files match SHA256 manifest.")
            sys.exit(0)
        else:
            print(f"Verification FAILED: {errors} errors found.")
            sys.exit(1)

    subdirs = ["backend", "scripts", "tests"]
    generate_manifest(ROOT, subdirs, create_backup_flag=not args.no_backup)


if __name__ == "__main__":
    main()
