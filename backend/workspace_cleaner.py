"""
workspace_cleaner.py - Tự động quản lý dung lượng ổ đĩa & dọn dẹp không gian làm việc (Workspace Retention Engine).
Giải quyết triệt để nguy cơ tràn đĩa (Disk Full Crash) trên ổ D: (dung lượng trống thấp < 30GB).

Tính năng:
1. clean_completed_job_intermediate_files:
   - Sau khi xác minh bản giao video, xóa bản render trùng và file tạm không còn cần thiết.
   - Bảo toàn audio checkpoint, manifest, QC, phụ đề và nguồn để tiếp tục hoặc sửa job.
2. clean_stale_workspace_jobs:
   - Quét và dọn dẹp các tệp trung gian của tất cả các job cũ đã hoàn tất trong D:\\workspace quá 24h.
3. clean_stale_downloads:
   - Dọn dẹp video tải thô Douyin/TikTok trong D:\\workspace\\downloads (vốn đang chiếm hơn 50GB)
     khi đã cũ hơn max_age_days hoặc khi dung lượng ổ D: dưới ngưỡng an toàn (min_free_gb).
"""

from __future__ import annotations

import shutil
import time
import logging
from pathlib import Path
from typing import Dict, Optional

logger = logging.getLogger(__name__)

# Các đuôi file và mẫu tên file trung gian dung lượng lớn cần dọn dẹp sau khi job xong
INTERMEDIATE_FILE_PATTERNS = [
    "*_fit_in.wav",
    "*_fit_out.wav",
    "*_fit_stage.mp3",
    "*.downloading",
    "*.part",
    "*.range",
]


def get_disk_free_gb(path: str | Path) -> float:
    """Trả về dung lượng trống tính theo GB của ổ đĩa chứa đường dẫn."""
    try:
        usage = shutil.disk_usage(str(path))
        return usage.free / (1024 ** 3)
    except Exception:
        return 999.0


def _job_manifests(workspace_dir):
    from v1_checkpoint import ManifestManager
    ws = Path(workspace_dir)
    directory = ws / "bot_system" / "manifests"
    if not directory.is_dir():
        return []
    manager = ManifestManager(ws)
    return [m for p in directory.glob("*.manifest.json")
            if (m := manager.load_manifest(p.name.removesuffix(".manifest.json"))) is not None]


def _verified_delivery(manifest):
    from dashboard_media import media_status
    from v1_checkpoint import fingerprint
    if manifest.status != "completed" or not manifest.output_video_path:
        return False
    temp_render = manifest.stages.get("video_rendering", {}).get("artifacts", {}).get("temp_render")
    expected = temp_render.get("sha256") if isinstance(temp_render, dict) else None
    try:
        return bool(expected and media_status(manifest.output_video_path)["status"] == "valid"
                    and fingerprint(manifest.output_video_path) == expected)
    except OSError:
        return False


def clean_completed_job_intermediate_files(job_id: str, workspace_dir: str | Path,
                                           job_directory: Optional[str | Path] = None) -> int:
    """Remove disposable scratch and duplicate final video; retain checkpoint media."""
    from v1_checkpoint import fingerprint
    ws = Path(workspace_dir).resolve()
    manifests = _job_manifests(ws)
    manifest = next((m for m in manifests if m.job_id == job_id), None)
    if not manifest or not _verified_delivery(manifest):
        return 0
    recorded_dir = manifest.effective_config.get("output_dir")
    if not recorded_dir:
        return 0
    job_dir = Path(recorded_dir).resolve()
    if (not job_dir.is_relative_to(ws) or job_dir == ws or not job_dir.is_dir()
            or (job_directory and Path(job_directory).resolve() != job_dir)):
        return 0
    for other in manifests:
        if (other.job_id != job_id and other.status != "completed"
                and other.effective_config.get("output_dir")
                and Path(other.effective_config["output_dir"]).resolve() == job_dir):
            return 0

    protected = {Path(manifest.video_path).resolve(), Path(manifest.output_video_path).resolve()}
    for stage in manifest.stages.values():
        if isinstance(stage, dict):
            for artifact in stage.get("artifacts", {}).values():
                if isinstance(artifact, dict) and artifact.get("path"):
                    protected.add(Path(artifact["path"]).resolve())
                elif isinstance(artifact, (str, Path)):
                    protected.add(Path(artifact).resolve())
    freed_bytes = 0
    for pattern in INTERMEDIATE_FILE_PATTERNS:
        for path in job_dir.rglob(pattern):
            try:
                if not path.is_file() or path.resolve() in protected or not path.resolve().is_relative_to(job_dir):
                    continue
                size = path.stat().st_size
                path.unlink()
                freed_bytes += size
            except OSError:
                continue

    # A workspace render is redundant only after the delivered copy matches it.
    for path in job_dir.glob("final_*.mp4"):
        try:
            if (path.resolve() == Path(manifest.output_video_path).resolve()
                    or path.resolve() == Path(manifest.video_path).resolve()
                    or not path.resolve().is_relative_to(job_dir)):
                continue
            expected = manifest.stages["video_rendering"]["artifacts"]["temp_render"]["sha256"]
            if fingerprint(path) == expected:
                size = path.stat().st_size
                path.unlink()
                freed_bytes += size
        except (OSError, KeyError):
            continue
    if freed_bytes:
        logger.info("[CLEANUP] Job %s: freed %.1f MB", job_id, freed_bytes / 1024**2)
    return freed_bytes


def clean_stale_workspace_jobs(workspace_dir: str | Path, max_age_hours: float = 24.0) -> int:
    """Only completed manifests older than the retention period may be cleaned."""
    now = time.time()
    return sum(clean_completed_job_intermediate_files(m.job_id, workspace_dir)
               for m in _job_manifests(workspace_dir)
               if m.status == "completed" and now - m.updated_at > max_age_hours * 3600)


def clean_stale_downloads(
    downloads_dir: str | Path,
    max_age_days: float = 2.0,
    min_free_gb: float = 40.0
) -> int:
    """
    Dọn dẹp các tệp video tải thô trong downloads_dir.
    Nếu dung lượng ổ đĩa < min_free_gb, chủ động dọn các file cũ nhất để duy trì an toàn.
    """
    dl_path = Path(downloads_dir)
    if not dl_path.is_dir():
        return 0

    freed_bytes = 0
    now = time.time()
    max_age_sec = max_age_days * 86400.0

    files = []
    for f in dl_path.iterdir():
        if f.is_file():
            try:
                st = f.stat()
                files.append((st.st_mtime, st.st_size, f))
            except OSError:
                pass

    # Sắp xếp file theo thời gian tăng dần (cũ nhất lên đầu)
    files.sort(key=lambda x: x[0])

    # Raw media belonging to unfinished jobs or queued requests must remain resumable.
    manifests = _job_manifests(dl_path.parent)
    protected = {Path(m.video_path).resolve() for m in manifests if m.status != "completed"}
    eligible = {Path(m.video_path).resolve(): m.video_fingerprint
                for m in manifests if _verified_delivery(m)}
    try:
        import json
        for snapshot in (dl_path.parent / "telegram_queue.json",
                         dl_path.parent / "bot_system" / "telegram_queue.json"):
            if snapshot.is_file():
                data = json.loads(snapshot.read_text(encoding="utf-8-sig"))
                jobs = list(data.get("items") or []) + [data.get("active_job") or {}]
                for job in jobs:
                    if isinstance(job, dict):
                        source = (job.get("resume_state") or {}).get("source_path")
                        if source:
                            protected.add(Path(source).resolve())
    except (OSError, ValueError, TypeError):
        return 0

    for mtime, size, file_path in files:
        if file_path.resolve() in protected:
            continue
        free_gb = get_disk_free_gb(dl_path)
        is_stale = (now - mtime) > max_age_sec
        is_disk_low = free_gb < min_free_gb

        # Xóa các file tải dở .downloading hoặc .part bất kể tuổi thọ nếu quá 6h
        if file_path.suffix in (".downloading", ".part") or ".range" in file_path.name:
            if (now - mtime) > 21600:
                try:
                    file_path.unlink()
                    freed_bytes += size
                except OSError:
                    pass
                continue

        # Xóa file video thô nếu đã cũ hoặc ổ đĩa sắp đầy
        if (is_stale or is_disk_low) and file_path.resolve() in eligible:
            try:
                from v1_checkpoint import fingerprint
                if fingerprint(file_path) != eligible[file_path.resolve()]:
                    continue
                file_path.unlink()
                freed_bytes += size
                logger.info(f"[CLEANUP] Đã xóa video thô cũ ({size / (1024**2):.1f} MB): {file_path.name}")
            except OSError:
                pass

        # Dừng lại nếu dung lượng trống đã đạt ngưỡng an toàn và đã quét hết file quá hạn
        if free_gb >= (min_free_gb + 10.0) and not is_stale:
            break

    if freed_bytes > 0:
        logger.info(f"[CLEANUP] Downloads: Đã giải phóng {freed_bytes / (1024**3):.2f} GB video thô.")
    return freed_bytes


def run_full_retention_maintenance(
    workspace_dir: str | Path = r"D:\workspace",
    downloads_dir: Optional[str | Path] = None,
    min_free_gb: float = 40.0
) -> Dict[str, Any]:
    """Chạy toàn bộ chu trình bảo trì và dọn dẹp workspace định kỳ."""
    ws = Path(workspace_dir)
    dl = Path(downloads_dir) if downloads_dir else ws / "downloads"
    before_free = get_disk_free_gb(ws)

    ws_freed = clean_stale_workspace_jobs(ws, max_age_hours=24.0)
    dl_freed = clean_stale_downloads(dl, max_age_days=2.0, min_free_gb=min_free_gb)
    after_free = get_disk_free_gb(ws)

    total_freed_gb = (ws_freed + dl_freed) / (1024 ** 3)
    logger.info(
        f"[CLEANUP MAINTENANCE] Hoàn tất. Đã giải phóng {total_freed_gb:.2f} GB. "
        f"Dung lượng trống ổ D: {before_free:.1f} GB -> {after_free:.1f} GB."
    )
    return {
        "status": "success",
        "freed_bytes": ws_freed + dl_freed,
        "freed_gb": round(total_freed_gb, 2),
        "disk_free_before_gb": round(before_free, 2),
        "disk_free_after_gb": round(after_free, 2),
    }


if __name__ == "__main__":
    import sys
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    res = run_full_retention_maintenance()
    print("Kết quả bảo trì dọn dẹp đĩa:", res)
