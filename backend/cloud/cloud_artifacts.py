"""Verify transfers before an artifact can enter the publication transaction."""
import hashlib
import json
import math
import os
from pathlib import Path
import shutil
import subprocess
import tempfile

from .cloud_contracts import CloudVerificationError


def sha256_file(path):
    digest = hashlib.sha256()
    with open(path, "rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def probe_media(path, timeout=30):
    try:
        result = subprocess.run(
            ["ffprobe", "-v", "error", "-show_streams", "-show_format", "-of", "json", str(path)],
            capture_output=True, text=True, timeout=timeout, check=False,
        )
        if result.returncode:
            raise CloudVerificationError("cloud_media_unreadable")
        return json.loads(result.stdout)
    except (OSError, subprocess.TimeoutExpired, ValueError) as exc:
        raise CloudVerificationError("cloud_media_probe_failed") from exc


def verify_artifact(path, manifest, job_id, attempt_id, source_sha256, probe=probe_media):
    path = Path(path)
    if not path.is_file() or path.is_symlink() or path.stat().st_size <= 0:
        raise CloudVerificationError("cloud_output_missing_or_empty")
    if (manifest.get("job_id"), manifest.get("attempt_id"), manifest.get("source_sha256")) != (job_id, attempt_id, source_sha256):
        raise CloudVerificationError("cloud_manifest_identity_mismatch")
    digest = sha256_file(path)
    if manifest.get("size_bytes") != path.stat().st_size or manifest.get("sha256") != digest:
        raise CloudVerificationError("cloud_manifest_hash_mismatch")
    media = probe(path)
    streams = media.get("streams", [])
    video = [s for s in streams if s.get("codec_type") == "video"]
    audio = [s for s in streams if s.get("codec_type") == "audio"]
    try:
        duration = float(media["format"]["duration"])
        expected = float(manifest["duration_seconds"])
        if not math.isfinite(duration) or not math.isfinite(expected) or min(duration, expected) <= 0:
            raise ValueError()
        if abs(duration - expected) > max(0.5, expected * 0.01):
            raise ValueError()
        if not video or not audio or int(video[0].get("width", 0)) <= 0 or int(video[0].get("height", 0)) <= 0:
            raise ValueError()
    except (KeyError, TypeError, ValueError) as exc:
        raise CloudVerificationError("cloud_media_contract_failed") from exc
    return {"sha256": digest, "size_bytes": path.stat().st_size, "duration_seconds": duration,
            "has_video": True, "has_audio": True}


def stage_for_publication(verified_path, output_dir, job_id, attempt_id, verification):
    output_dir = Path(output_dir).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    stage_dir = output_dir / ".cloud-staging" / job_id / attempt_id
    stage_dir.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix="verified-", suffix=".part", dir=stage_dir)
    try:
        with os.fdopen(fd, "wb") as target, open(verified_path, "rb") as source:
            shutil.copyfileobj(source, target)
            target.flush()
            os.fsync(target.fileno())
        if sha256_file(name) != verification["sha256"]:
            raise CloudVerificationError("cloud_staging_hash_mismatch")
        return Path(name)
    except BaseException:
        Path(name).unlink(missing_ok=True)
        raise
