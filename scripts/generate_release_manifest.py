"""Generate SHA256 release manifest, perform security hygiene check, and optionally create release backup.

Shares the canonical file selection policy with package_v1_release.py so that
the ZIP archive and SHA256 manifest always cover the exact same supported inventory.
"""

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
import tempfile
from typing import Dict, List, Tuple

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from package_v1_release import (  # noqa: E402
    EXCLUDE_DIRS,
    EXCLUDE_EXTENSIONS,
    EXCLUDE_FILES as EXCLUDE_FILENAMES,
    iter_release_files,
    should_include,
)

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


def should_include_file(rel_path: Path | str) -> bool:
    return should_include(rel_path)


def collect_release_files(root: Path, target_subdirs: List[str] | None = None) -> List[Path]:
    """Collect release files using the unified policy in package_v1_release.py."""
    all_files = iter_release_files(root)
    if not target_subdirs:
        return all_files
    allowed_roots = set(target_subdirs)
    filtered: List[Path] = []
    for rel in all_files:
        parts = rel.parts
        if len(parts) == 1 or parts[0] in allowed_roots:
            filtered.append(rel)
    return sorted(filtered, key=lambda p: str(p).replace("\\", "/"))


def check_secrets_hygiene(root: Path, files: List[Path]) -> List[Tuple[Path, str]]:
    from package_v1_release import _load_configured_secrets

    configured_secrets = _load_configured_secrets(root)
    violations = []
    for rel in files:
        full_path = root / rel
        try:
            raw_bytes = full_path.read_bytes()
            content = raw_bytes.decode("utf-8", errors="ignore")
        except Exception:
            continue

        if any(secret in raw_bytes for secret in configured_secrets):
            violations.append((rel, "Found configured .env credential value"))
            continue

        for pat in SENSITIVE_PATTERNS:
            match = pat.search(content)
            if match:
                matched_str = match.group(0)
                if "test" in str(rel).lower() and any(
                    marker in matched_str.lower()
                    for marker in ("dummy", "mock", "example", "test", "synthetic", "secret_", "fake")
                ):
                    continue
                violations.append((rel, f"Found pattern match: {matched_str[:8]}..."))
    return violations


def create_backup(root: Path, files: List[Path], timestamp: str) -> Path:
    backup_dir = root / "backups" / f"v1-release-{timestamp}"
    backup_dir.mkdir(parents=True, exist_ok=True)

    for rel in files:
        src = root / rel
        dst = backup_dir / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)

    return backup_dir


def test_evidence(log_path: Path | None) -> Dict[str, object]:
    """Parse real test evidence from either pytest or unittest log output (supports UTF-8 and UTF-16 PowerShell logs)."""
    if log_path is None:
        return {"status": "NOT_RUN", "count": None}
    raw = log_path.read_bytes()
    if raw.startswith((b"\xff\xfe", b"\xfe\xff")):
        text = raw.decode("utf-16", errors="replace")
    else:
        text = raw.decode("utf-8-sig", errors="replace")

    # Reject any log containing explicit test failures or errors
    if (
        re.search(r"^FAILED\b", text, re.MULTILINE)
        or re.search(r"\b(\d+)\s+failed\b", text)
        or re.search(r"\b(\d+)\s+errors?\b", text)
    ):
        raise ValueError("Test log contains failures or errors")

    # 1. Check pytest summary line: e.g. "1216 passed, 79 skipped, 4 warnings in 118.66s"
    pytest_matches = list(
        re.finditer(
            r"(?:=+\s*)?\b(\d+)\s+passed\b(?:,\s*(\d+)\s+skipped)?[^\r\n]*?\bin\s+[0-9.]+s",
            text,
        )
    )
    if pytest_matches:
        last_match = pytest_matches[-1]
        passed = int(last_match.group(1))
        skipped = int(last_match.group(2) or 0)
        if passed <= 0:
            raise ValueError("Pytest log reported 0 passed tests")
        status_str = f"{passed} passed, {skipped} skipped" if skipped else f"{passed}/{passed} PASS"
        return {
            "runner": "pytest",
            "status": status_str,
            "count": passed,
            "passed": passed,
            "skipped": skipped,
            "log_path": str(log_path.resolve()),
            "log_sha256": hash_file_sha256(log_path),
        }

    # 2. Check unittest summary line: e.g. "Ran 8 tests in 1.234s\n\nOK"
    unittest_match = re.search(r"Ran (\d+) tests? in [0-9.]+s\s+OK\s*(?:\n|$)", text)
    if unittest_match:
        count = int(unittest_match.group(1))
        return {
            "runner": "unittest",
            "status": f"{count}/{count} PASS",
            "count": count,
            "passed": count,
            "skipped": 0,
            "log_path": str(log_path.resolve()),
            "log_sha256": hash_file_sha256(log_path),
        }

    raise ValueError("Test log does not prove a successful completed suite")


def _atomic_write_text(target: Path, content: str) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{target.name}_", suffix=".tmp", dir=str(target.parent))
    os.close(fd)
    tmp_path = Path(tmp_name)
    try:
        tmp_path.write_text(content, encoding="utf-8")
        tmp_path.replace(target)
    except Exception:
        tmp_path.unlink(missing_ok=True)
        raise


def generate_manifest(
    root: Path,
    target_subdirs: List[str] | None = None,
    create_backup_flag: bool = False,
    test_log: Path | None = None,
    release_tag: str = "v1-py312-verified",
    output_dir: Path | None = None,
    dry_run: bool = False,
) -> Dict[str, object]:
    evidence = test_evidence(test_log)
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
        print("Security hygiene check: PASS (No tokens or API keys found in release files)")

    manifest_entries: List[str] = []
    manifest_dict: Dict[str, str] = {}
    for rel in files:
        full = root / rel
        sha256 = hash_file_sha256(full)
        norm_path = str(rel).replace("\\", "/")
        manifest_entries.append(f"{sha256}  {norm_path}")
        manifest_dict[norm_path] = sha256

    out_base = (output_dir or root).resolve()
    manifest_file = out_base / "RELEASE_MANIFEST.sha256"
    meta_file = out_base / "RELEASE_METADATA.json"

    backup_path = None
    if not dry_run and create_backup_flag:
        backup_path = create_backup(root, files, timestamp)
        print(f"Created release backup at: {backup_path}")

    metadata: Dict[str, object] = {
        "timestamp": timestamp,
        "release_tag": release_tag,
        "file_count": len(files),
        "manifest_sha256_file": manifest_file.name,
        "backup_dir": str(backup_path.relative_to(root)) if backup_path else None,
        "test_suite_status": evidence["status"],
        "test_evidence": evidence,
        "security_hygiene": "PASS",
        "dry_run": dry_run,
    }

    if dry_run:
        metadata["files"] = list(manifest_dict.keys())
        print(f"Dry-run complete: {len(manifest_entries)} manifest entries validated (no files written).")
        return metadata

    _atomic_write_text(manifest_file, "\n".join(manifest_entries) + "\n")
    print(f"Wrote SHA256 manifest to: {manifest_file} ({len(manifest_entries)} entries)")

    _atomic_write_text(meta_file, json.dumps(metadata, indent=2, ensure_ascii=False))
    print(f"Wrote release metadata to: {meta_file}")

    return metadata


def main(argv: List[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Generate SHA256 Release Manifest for Tool V1")
    parser.add_argument("--root", type=Path, default=ROOT, help="Source repository root")
    parser.add_argument("--output-dir", type=Path, default=None, help="Directory to write manifest and metadata")
    parser.add_argument("--backup", action="store_true", help="Create release backup copy in backups/")
    parser.add_argument("--no-backup", action="store_true", help="Skip creating release backup (default)")
    parser.add_argument("--dry-run", action="store_true", help="Validate inventory and secrets without writing files")
    parser.add_argument("--verify", action="store_true", help="Verify existing manifest against files")
    parser.add_argument("--test-log", type=Path, help="Completed pytest or unittest log")
    parser.add_argument("--release-tag", default="v1-py312-verified")
    args = parser.parse_args(argv)

    root_dir = args.root.resolve()
    out_dir = (args.output_dir or root_dir).resolve()

    if args.verify:
        manifest_file = out_dir / "RELEASE_MANIFEST.sha256"
        if not manifest_file.exists():
            print(f"Error: {manifest_file} does not exist.")
            return 1
        lines = manifest_file.read_text(encoding="utf-8").splitlines()
        errors = 0
        for line in lines:
            if not line.strip():
                continue
            expected_sha, rel_str = line.split("  ", 1)
            target = root_dir / rel_str
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
            return 0
        print(f"Verification FAILED: {errors} errors found.")
        return 1

    generate_manifest(
        root_dir,
        target_subdirs=None,
        create_backup_flag=bool(args.backup and not args.no_backup),
        test_log=args.test_log,
        release_tag=args.release_tag,
        output_dir=out_dir,
        dry_run=args.dry_run,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
