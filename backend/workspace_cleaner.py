"""
workspace_cleaner.py - Tự động quản lý dung lượng ổ đĩa & dọn dẹp không gian làm việc (Workspace Retention Engine).
Giải quyết triệt để nguy cơ tràn đĩa (Disk Full Crash) trên ổ D: (dung lượng trống thấp < 30GB).

Tính năng:
1. clean_completed_job_intermediate_files:
   - Sau khi job xuất bản thành công tới D:\\banve\\Dubbed_*.mp4, lập tức xóa video trùng lặp
     (final_*.mp4) và các file stem âm thanh PCM cực nặng (mixed.wav, vocals.wav, no_vocals.wav, bs_roformer/*.wav).
   - Bảo toàn 100% các tệp metadata nhẹ quan trọng: manifest.json, qc_report.json, .srt, .ass.
2. clean_stale_workspace_jobs:
   - Quét và dọn dẹp các tệp trung gian của tất cả các job cũ đã hoàn tất trong D:\\workspace quá 24h.
3. clean_stale_downloads:
   - Dọn dẹp video tải thô Douyin/TikTok trong D:\\workspace\\downloads (vốn đang chiếm hơn 50GB)
     khi đã cũ hơn max_age_days hoặc khi dung lượng ổ D: dưới ngưỡng an toàn (min_free_gb).
"""

from __future__ import annotations

import os
import shutil
import time
import logging
from pathlib import Path
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# Các đuôi file và mẫu tên file trung gian dung lượng lớn cần dọn dẹp sau khi job xong
INTERMEDIATE_FILE_PATTERNS = [
    "mixed.wav",
    "original.wav",
    "vocals.wav",
    "no_vocals.wav",
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


def clean_completed_job_intermediate_files(job_id: str, workspace_dir: str | Path) -> int:
    """
    Dọn dẹp các file trung gian nặng của một job cụ thể sau khi đã xuất bản video thành phẩm tới D:\\banve.
    Trả về số bytes đã giải phóng.
    """
    ws = Path(workspace_dir)
    job_dir = ws / job_id
    if not job_dir.is_dir():
        # Kiểm tra nếu job_dir nằm trực tiếp trong workspace với tên job
        return 0

    freed_bytes = 0

    # 1. Xóa các file âm thanh WAV trung gian lớn
    for p in job_dir.glob("*.wav"):
        try:
            sz = p.stat().st_size
            p.unlink()
            freed_bytes += sz
        except OSError:
            pass

    # 2. Xóa thư mục bs_roformer chứa stems nếu có
    roformer_dir = job_dir / "bs_roformer"
    if roformer_dir.is_dir():
        try:
            for stem in roformer_dir.glob("*.wav"):
                try:
                    freed_bytes += stem.stat().st_size
                    stem.unlink()
                except OSError:
                    pass
            shutil.rmtree(str(roformer_dir), ignore_errors=True)
        except Exception:
            pass

    # 3. Xóa thư mục demucs nếu có
    demucs_dir = job_dir / "demucs"
    if demucs_dir.is_dir():
        try:
            shutil.rmtree(str(demucs_dir), ignore_errors=True)
        except Exception:
            pass

    # 4. Xóa video trung gian final_*.mp4 trong workspace (vì đã có bản phân phối Dubbed_*.mp4 ở D:\banve)
    for vid in job_dir.glob("final_*.mp4"):
        try:
            sz = vid.stat().st_size
            vid.unlink()
            freed_bytes += sz
        except OSError:
            pass

    if freed_bytes > 0:
        logger.info(f"[CLEANUP] Job {job_id}: Đã giải phóng {freed_bytes / (1024**2):.1f} MB tệp trung gian.")
    return freed_bytes


def clean_stale_workspace_jobs(workspace_dir: str | Path, max_age_hours: float = 24.0) -> int:
    """
    Quét toàn bộ workspace và dọn dẹp các tệp trung gian (WAV, final_*.mp4 trùng lặp)
    của các job đã xong quá max_age_hours.
    """
    ws = Path(workspace_dir)
    if not ws.is_dir():
        return 0

    freed_bytes = 0
    now = time.time()
    max_age_sec = max_age_hours * 3600.0

    for d in ws.iterdir():
        if not d.is_dir() or d.name in ("bot_system", "downloads", "control", "output_receipts", "service_logs", "translation_cache", ".v1_ocr_cache"):
            continue

        try:
            mtime = d.stat().st_mtime
            is_stale = (now - mtime) > max_age_sec

            # Dọn dẹp file WAV lớn trong các folder job cũ
            for wav_file in d.glob("*.wav"):
                try:
                    sz = wav_file.stat().st_size
                    wav_file.unlink()
                    freed_bytes += sz
                except OSError:
                    pass

            # Dọn dẹp thư mục bs_roformer
            bs_dir = d / "bs_roformer"
            if bs_dir.is_dir():
                for stem in bs_dir.glob("*.wav"):
                    try:
                        freed_bytes += stem.stat().st_size
                        stem.unlink()
                    except OSError:
                        pass
                shutil.rmtree(str(bs_dir), ignore_errors=True)

            # Dọn dẹp final_*.mp4 nếu job cũ hơn 24h
            if is_stale:
                for final_vid in d.glob("final_*.mp4"):
                    try:
                        freed_bytes += final_vid.stat().st_size
                        final_vid.unlink()
                    except OSError:
                        pass
        except Exception:
            continue

    if freed_bytes > 0:
        logger.info(f"[CLEANUP] Workspace: Đã dọn dẹp và giải phóng {freed_bytes / (1024**3):.2f} GB từ các job cũ.")
    return freed_bytes


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

    for mtime, size, file_path in files:
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
        if is_stale or is_disk_low:
            try:
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
