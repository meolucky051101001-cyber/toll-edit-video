"""Archive the installed, RECORD-verified local CapCut package without network access."""
from __future__ import annotations

import argparse
import base64
import csv
import hashlib
import importlib.metadata as metadata
import io
from pathlib import Path, PurePosixPath
import re
import sys
import zipfile


def export_capcut_wheel(output_dir: Path) -> Path:
    distribution = metadata.distribution("capcut-tts-api")
    version = distribution.version
    if not re.fullmatch(r"[A-Za-z0-9_.+]+", version):
        raise ValueError("Invalid installed package version")
    wheel = distribution.read_text("WHEEL") or ""
    tags = [line.removeprefix("Tag:").strip() for line in wheel.splitlines() if line.startswith("Tag:")]
    if "Root-Is-Purelib: true" not in wheel or tags != ["py3-none-any"]:
        raise ValueError("Only the verified pure-Python CapCut wheel can be archived")
    allowed_roots = {"capcut_tts_api", f"capcut_tts_api-{version}.dist-info"}
    excluded = {"RECORD", "INSTALLER", "REQUESTED", "direct_url.json"}
    files = {}
    for entry in distribution.files or []:
        relative = PurePosixPath(entry.as_posix())
        if ".." in relative.parts or "__pycache__" in relative.parts:
            continue  # pip-generated console executables and bytecode are recreated on install.
        if relative.is_absolute() or not relative.parts or relative.parts[0] not in allowed_roots:
            raise ValueError("Unexpected file outside the CapCut distribution")
        if relative.name in excluded:
            continue
        content = distribution.locate_file(entry).read_bytes()
        record_hash = entry.hash
        if record_hash is None or record_hash.mode != "sha256":
            raise ValueError("A package file has no verifiable SHA256 RECORD entry")
        actual = base64.urlsafe_b64encode(hashlib.sha256(content).digest()).rstrip(b"=").decode("ascii")
        if actual != record_hash.value or entry.size is not None and len(content) != entry.size:
            raise ValueError("Installed CapCut bytes differ from their recorded hash; archive refused")
        files[relative.as_posix()] = content
    dist_info = f"capcut_tts_api-{version}.dist-info"
    if not all(name in files for name in [f"{dist_info}/WHEEL", f"{dist_info}/METADATA", "capcut_tts_api/__init__.py"]):
        raise ValueError("The installed CapCut distribution is incomplete")
    record = io.StringIO(newline="")
    writer = csv.writer(record, lineterminator="\n")
    for name, content in sorted(files.items()):
        digest = base64.urlsafe_b64encode(hashlib.sha256(content).digest()).rstrip(b"=").decode("ascii")
        writer.writerow([name, "sha256=" + digest, len(content)])
    record_name = f"{dist_info}/RECORD"
    writer.writerow([record_name, "", ""])
    files[record_name] = record.getvalue().encode("utf-8")
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, content in sorted(files.items()):
            item = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
            item.compress_type = zipfile.ZIP_DEFLATED
            item.external_attr = 0o644 << 16
            archive.writestr(item, content)
    output_dir.mkdir(parents=True, exist_ok=True)
    target = output_dir / f"capcut_tts_api-{version}-py3-none-any.whl"
    payload = buffer.getvalue()
    if target.exists():
        if target.read_bytes() != payload:
            raise FileExistsError("A different wheel already exists; no file was overwritten")
    else:
        with target.open("xb") as stream:
            stream.write(payload)
    return target


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", default="backups/wheels")
    options = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    if sys.version_info[:2] != (3, 12) or Path(sys.prefix).resolve() != (root / "backend/venv_py312").resolve():
        raise SystemExit("Run only with backend/venv_py312/Scripts/python.exe")
    destination = (root / options.output_dir).resolve()
    if not destination.is_relative_to(root):
        raise SystemExit("Wheel archive must stay inside the project")
    path = export_capcut_wheel(destination)
    print("Verified local package archived:", path)
    print("SHA256:", hashlib.sha256(path.read_bytes()).hexdigest())
