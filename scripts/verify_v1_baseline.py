#!/usr/bin/env python3
"""Cryptographic baseline verification script for Tool V1.

Verifies that Tool V1 at 'C:\\tool v1':
1. Git HEAD points to commit: c822af952d4fdd8ddbe72ffb27f8863a87a0b6b6
2. Git commit tree hash matches: 23b0461837b9317f783c5804635d3ed5c4005fa3
3. All 267 tracked blobs match their cryptographic SHA-256 baseline in V1_BASELINE_MANIFEST.sha256.
"""

from __future__ import annotations

import argparse
import hashlib
from pathlib import Path
import subprocess
import sys
from typing import Dict, List, Tuple

EXPECTED_COMMIT = "c822af952d4fdd8ddbe72ffb27f8863a87a0b6b6"
EXPECTED_TREE = "23b0461837b9317f783c5804635d3ed5c4005fa3"


def parse_manifest(manifest_path: Path) -> Dict[str, str]:
    entries: Dict[str, str] = {}
    if not manifest_path.is_file():
        raise FileNotFoundError(f"Manifest not found: {manifest_path}")
    text = manifest_path.read_text(encoding="utf-8")
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split(maxsplit=1)
        if len(parts) == 2:
            entries[parts[1].strip()] = parts[0].strip().lower()
    return entries


def load_blobs_batch(v1_dir: Path, blob_shas: Dict[str, str]) -> Dict[str, bytes]:
    sha_to_paths: Dict[str, List[str]] = {}
    for rpath, bsha in blob_shas.items():
        sha_to_paths.setdefault(bsha, []).append(rpath)

    unique_shas = list(sha_to_paths.keys())
    proc = subprocess.Popen(
        ["git", "-C", str(v1_dir), "cat-file", "--batch"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
    )
    input_data = ("\n".join(unique_shas) + "\n").encode("ascii")
    stdout_bytes, _ = proc.communicate(input_data)

    blobs_by_sha: Dict[str, bytes] = {}
    offset = 0
    total_len = len(stdout_bytes)
    while offset < total_len:
        newline_idx = stdout_bytes.find(b"\n", offset)
        if newline_idx == -1:
            break
        header = stdout_bytes[offset:newline_idx].decode("ascii", errors="replace")
        parts = header.split()
        if len(parts) >= 3 and parts[1] == "blob":
            sha = parts[0]
            size = int(parts[2])
            start = newline_idx + 1
            content = stdout_bytes[start : start + size]
            blobs_by_sha[sha] = content
            offset = start + size + 1
        else:
            offset = newline_idx + 1

    path_to_content: Dict[str, bytes] = {}
    for rpath, bsha in blob_shas.items():
        if bsha in blobs_by_sha:
            path_to_content[rpath] = blobs_by_sha[bsha]
    return path_to_content


def verify_v1_baseline(
    v1_dir: Path,
    manifest_path: Path,
    check_disk: bool = False,
) -> bool:
    print("=" * 70)
    print("TOOL V1 CRYPTOGRAPHIC BASELINE VERIFICATION")
    print("=" * 70)
    print(f"Target V1 Directory: {v1_dir}")
    print(f"Baseline Manifest:   {manifest_path}")

    if not (v1_dir / ".git").exists():
        print(f"ERROR: {v1_dir} is not a git repository!")
        return False

    # 1. Check Git Commit
    try:
        commit_res = subprocess.check_output(
            ["git", "-C", str(v1_dir), "rev-parse", "HEAD"],
            text=True,
            timeout=10,
        ).strip()
    except Exception as exc:
        print(f"ERROR: Failed to query git commit: {exc}")
        return False

    if commit_res != EXPECTED_COMMIT:
        print(f"[FAIL] Git commit mismatch!")
        print(f"  Expected: {EXPECTED_COMMIT}")
        print(f"  Actual:   {commit_res}")
        return False
    print(f"[PASS] Git Commit: {commit_res}")

    # 2. Check Git Tree Hash
    try:
        tree_res = subprocess.check_output(
            ["git", "-C", str(v1_dir), "rev-parse", "HEAD^{tree}"],
            text=True,
            timeout=10,
        ).strip()
    except Exception as exc:
        print(f"ERROR: Failed to query git tree: {exc}")
        return False

    if tree_res != EXPECTED_TREE:
        print(f"[FAIL] Git tree hash mismatch!")
        print(f"  Expected: {EXPECTED_TREE}")
        print(f"  Actual:   {tree_res}")
        return False
    print(f"[PASS] Git Tree Hash: {tree_res}")

    # 3. Load Manifest
    manifest_entries = parse_manifest(manifest_path)
    print(f"[INFO] Manifest contains {len(manifest_entries)} tracked file baselines")

    # 4. Check Git Tree Blobs against manifest
    try:
        ls_tree_out = subprocess.check_output(
            ["git", "-C", str(v1_dir), "ls-tree", "-r", EXPECTED_TREE],
            text=True,
            timeout=30,
        )
    except Exception as exc:
        print(f"ERROR: Failed to inspect git tree: {exc}")
        return False

    tree_blobs: Dict[str, str] = {}
    for line in ls_tree_out.splitlines():
        if not line.strip():
            continue
        parts = line.split(maxsplit=3)
        if len(parts) == 4 and parts[1] == "blob":
            tree_blobs[parts[3].strip()] = parts[2].strip()

    print(f"[INFO] Tree contains {len(tree_blobs)} blob objects")

    # Batch load all pristine blobs from git object store
    path_to_content = load_blobs_batch(v1_dir, tree_blobs)

    blob_mismatches = 0
    checked_count = 0
    for rel_path, expected_sha256 in manifest_entries.items():
        if rel_path not in tree_blobs or rel_path not in path_to_content:
            print(f"[FAIL] File missing from git tree: {rel_path}")
            blob_mismatches += 1
            continue

        content = path_to_content[rel_path]
        actual_sha256 = hashlib.sha256(content).hexdigest()
        if actual_sha256 != expected_sha256:
            print(f"[FAIL] SHA-256 mismatch for {rel_path}")
            print(f"  Expected: {expected_sha256}")
            print(f"  Actual:   {actual_sha256}")
            blob_mismatches += 1
        else:
            checked_count += 1

    if blob_mismatches > 0:
        print(f"[FAIL] {blob_mismatches} blob(s) failed cryptographic verification!")
        return False

    print(f"[PASS] Git Object Store Baseline: All {checked_count}/{len(manifest_entries)} Git tree blob objects verified matching SHA-256 exactly (pristine).")

    # 5. Working Tree / Disk Check
    if check_disk:
        print("-" * 70)
        print("WORKING TREE DISK STATUS VERIFICATION (--check-disk)")
        print("-" * 70)
        exact_matches = 0
        crlf_matches = 0
        modified_diffs = []
        missing_files = []

        for rel_path, expected_sha256 in manifest_entries.items():
            fpath = v1_dir / rel_path
            if not fpath.is_file():
                missing_files.append(rel_path)
                continue

            disk_bytes = fpath.read_bytes()
            disk_sha = hashlib.sha256(disk_bytes).hexdigest()
            if disk_sha == expected_sha256:
                exact_matches += 1
                continue

            # Compare with CRLF normalized
            blob_bytes = path_to_content.get(rel_path, b"")
            if disk_bytes.replace(b"\r\n", b"\n") == blob_bytes.replace(b"\r\n", b"\n"):
                crlf_matches += 1
            else:
                modified_diffs.append(rel_path)

        total_files = len(manifest_entries)
        print(f"[INFO] Working Tree Disk Breakdown:")
        print(f"       - Exact byte matches:        {exact_matches}/{total_files}")
        print(f"       - CRLF-only differences:     {crlf_matches}/{total_files} (Windows checkout line endings)")
        print(f"       - Content modified on disk:  {len(modified_diffs)}/{total_files}")
        print(f"       - Missing files on disk:     {len(missing_files)}/{total_files}")

        if modified_diffs or missing_files:
            print(f"[FAIL] Working tree is NOT pristine. Detected active modifications or runtime state:")
            for item in modified_diffs[:10]:
                print(f"       - Modified: {item}")
            for item in missing_files[:5]:
                print(f"       - Missing:  {item}")
            print("=" * 70)
            print("RESULT: DISK WORKING TREE CHECK FAILED (FAIL-CLOSED)")
            print("=" * 70)
            return False

        print(f"[PASS] Working tree on disk has NO content modifications (all {total_files} files match content).")

    print("=" * 70)
    print("RESULT: TOOL V1 BASELINE IS VALID AND CRYPTOGRAPHICALLY SECURE")
    print("=" * 70)
    return True


def main() -> int:
    parser = argparse.ArgumentParser(description="Verify Tool V1 cryptographic baseline")
    parser.add_argument(
        "--v1-path",
        type=Path,
        default=Path("C:/tool v1"),
        help="Path to Tool V1 directory",
    )
    parser.add_argument(
        "--manifest",
        type=Path,
        default=Path(__file__).resolve().parents[1] / "V1_BASELINE_MANIFEST.sha256",
        help="Path to V1_BASELINE_MANIFEST.sha256",
    )
    parser.add_argument(
        "--check-disk",
        action="store_true",
        help="Also check disk working tree files",
    )
    args = parser.parse_args()

    success = verify_v1_baseline(args.v1_path, args.manifest, check_disk=args.check_disk)
    return 0 if success else 1


if __name__ == "__main__":
    sys.exit(main())
