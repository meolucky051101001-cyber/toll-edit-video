"""Dashboard checks only: bounded ffprobe cache, no AI imports or GPU work."""
import json
import math
import os
import subprocess
import threading
import time
import unicodedata
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

_lock = threading.RLock()
_cache = OrderedDict()
_delivery_cache = OrderedDict()
_pending = {}
_pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="dashboard-media")


def _signature(path):
    p = Path(path)
    s = p.stat()
    return (os.path.normcase(str(p.resolve())), s.st_size, s.st_mtime_ns)


def _probe(path):
    try:
        r = subprocess.run(
            ["ffprobe", "-v", "error", "-show_streams", "-show_format", "-of", "json", str(path)],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=5,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        if r.returncode:
            return {"status": "invalid", "reason": "Không đọc được nội dung video/âm thanh."}
        data = json.loads(r.stdout)
        streams = data.get("streams", [])
        video = any(s.get("codec_type") == "video" and int(s.get("width", 0)) > 0 and int(s.get("height", 0)) > 0 for s in streams)
        audio = any(s.get("codec_type") == "audio" and int(s.get("channels", 0)) > 0 for s in streams)
        duration = float(data.get("format", {}).get("duration", 0) or 0)
        if not (video and audio and math.isfinite(duration) and duration > 0.05):
            return {"status": "invalid", "reason": "Thiếu luồng hình/âm thanh hoặc thời lượng hợp lệ."}
        return {"status": "valid", "reason": "Đã xác minh luồng hình, âm thanh và thời lượng; chưa kiểm tra nội dung/QC."}
    except FileNotFoundError:
        return {"status": "unverified", "reason": "Chưa xác minh: không tìm thấy ffprobe."}
    except subprocess.TimeoutExpired:
        return {"status": "unverified", "reason": "Chưa xác minh: ffprobe quá thời gian chờ."}
    except (OSError, ValueError, TypeError):
        return {"status": "unverified", "reason": "Chưa xác minh: lỗi đọc thông tin media."}


def media_status(path, defer=False):
    """Invalidate on size/mtime changes; defer expensive list checks off request threads."""
    try:
        key = _signature(path)
    except OSError:
        return {"status": "invalid", "reason": "File đầu ra không còn tồn tại hoặc không đọc được."}
    if key[1] == 0:
        return {"status": "invalid", "reason": "File đầu ra rỗng (0 byte)."}
    with _lock:
        entry = _cache.get(key)
        if entry and time.monotonic() - entry[0] < (300 if entry[1]["status"] != "unverified" else 15):
            _cache.move_to_end(key)
            return dict(entry[1])
        future = _pending.get(key)
        if future and future.done():
            result = future.result()
            _pending.pop(key, None)
            _cache[key] = (time.monotonic(), result)
            while len(_cache) > 2048:
                _cache.popitem(last=False)
            return dict(result)
        if defer:
            # Bound queued work; next polling pass can enqueue remaining files.
            for old_key, task in list(_pending.items()):
                if task.done():
                    _cache[old_key] = (time.monotonic(), task.result())
                    _pending.pop(old_key, None)
            while len(_cache) > 2048:
                _cache.popitem(last=False)
            if key not in _pending and len(_pending) < 64:
                _pending[key] = _pool.submit(_probe, Path(path))
            return {"status": "unverified", "reason": "Đang chờ xác minh media."}
    try:
        result = future.result(timeout=6) if future else _probe(Path(path))
    except TimeoutError:
        return {"status": "unverified", "reason": "Đang chờ xác minh media."}
    with _lock:
        _pending.pop(key, None)
        _cache[key] = (time.monotonic(), result)
        while len(_cache) > 2048:
            _cache.popitem(last=False)
    return dict(result)


def _verify_dashboard_delivery(path, expected_source_path=None):
    """Use the existing QC/manifest gate plus cached media verification, never fail open."""
    from pipeline_v2.delivery_verification import verify_delivered_product
    result = verify_delivered_product(path, expected_source_path=expected_source_path,
                                      check_sha256=False, verify_media_streams=False)
    if not result.is_valid:
        return {"valid": False, "pending": False, "reason": result.reason}
    if expected_source_path:
        source = result.manifest.metadata.get("source_path")
        if not source or os.path.normcase(str(Path(source).resolve())) != os.path.normcase(str(Path(expected_source_path).resolve())):
            return {"valid": False, "pending": False, "reason": "Manifest thuộc video nguồn khác; chưa đối chiếu SHA-256."}
        try:
            manifest_path = Path(path)
            if manifest_path.is_dir():
                manifest_path = manifest_path / "job_manifest.json"
                if not manifest_path.exists():
                    manifest_path = Path(path) / "pipeline_v2" / "job_manifest.json"
            if Path(expected_source_path).stat().st_mtime_ns > manifest_path.stat().st_mtime_ns:
                return {"valid": False, "pending": False, "reason": "Video nguồn đã thay đổi sau lần xử lý được ghi nhận."}
        except OSError:
            return {"valid": False, "pending": False, "reason": "Không đọc được video nguồn hoặc manifest."}
    checked_media = False
    for item in result.published_outputs:
        p = Path(item["path"])
        if p.suffix.lower() not in {".mp4", ".mkv", ".mov", ".webm", ".avi", ".m4v"}:
            continue
        checked_media = True
        check = media_status(p)
        if check["status"] != "valid":
            return {"valid": False, "pending": check["status"] == "unverified", "reason": check["reason"]}
    if not checked_media:
        return {"valid": False, "pending": False, "reason": "Không có video thành phẩm trong danh sách xuất bản."}
    return {"valid": True, "pending": False, "reason": result.reason}


def _file_stamp(path):
    try:
        s = Path(path).stat()
        return (s.st_size, s.st_mtime_ns)
    except OSError:
        return None


def verify_dashboard_delivery(path, expected_source_path=None):
    manifest = Path(path)
    if manifest.is_dir():
        manifest = manifest / "job_manifest.json"
        if not manifest.is_file():
            manifest = Path(path) / "pipeline_v2" / "job_manifest.json"
    key = (os.path.normcase(str(manifest.resolve())), str(expected_source_path or ""))
    with _lock:
        entry = _delivery_cache.get(key)
        if entry and time.monotonic() - entry[0] < (1 if entry[3].get("pending") else 10):
            if tuple(_file_stamp(p) for p in entry[1]) == entry[2]:
                _delivery_cache.move_to_end(key)
                return dict(entry[3])
    paths = [manifest, manifest.parent / "artifacts" / "qc" / "qc_report.json"]
    if expected_source_path:
        paths.append(Path(expected_source_path))
    try:
        data = json.loads(manifest.read_text(encoding="utf-8"))
        meta = data.get("stages", {}).get("deliver", {}).get("metadata", {})
        for item in (meta.get("published_outputs") or [meta.get("published_output")]):
            if isinstance(item, dict) and item.get("path"):
                paths.append(Path(item["path"]))
    except (OSError, ValueError, TypeError, AttributeError):
        pass
    before = tuple(_file_stamp(p) for p in paths)
    result = _verify_dashboard_delivery(path, expected_source_path)
    # A concurrent publish must not stamp an earlier result with newer file metadata.
    if before == tuple(_file_stamp(p) for p in paths):
        with _lock:
            _delivery_cache[key] = (time.monotonic(), paths, before, dict(result))
            while len(_delivery_cache) > 1024:
                _delivery_cache.popitem(last=False)
    return result


def paginate_listing(data, limit=None, offset=0, search="", status="all"):
    def fold(value):
        return "".join(c for c in unicodedata.normalize("NFD", str(value).lower().replace("đ", "d")) if unicodedata.category(c) != "Mn")
    query = fold(search).strip()
    files = [f for f in data["files"] if (not query or query in fold(f["name"])) and (status == "all" or f.get("status") == status)]
    offset = max(0, offset)
    count = len(files)
    if limit is not None:
        limit = max(1, min(limit, 200))
        offset = min(offset, max(0, ((count - 1) // limit) * limit)) if count else 0
        files = files[offset:offset + limit]
    data.update(files=files, filtered_count=count, offset=offset, limit=limit,
                has_more=limit is not None and offset + len(files) < count)
    return data


def paginate_output(data, root, limit=None, offset=0, search="", status="all"):
    result = paginate_listing(data, limit, offset, search, "all") if status == "all" else data
    for item in result["files"]:
        check = media_status(Path(root) / item["name"], defer=True)
        item.update(status={"valid": "completed", "invalid": "invalid", "unverified": "unverified"}[check["status"]],
                    status_label={"valid": "Media hợp lệ", "invalid": "Đầu ra lỗi", "unverified": "Chưa xác minh"}[check["status"]],
                    verification_reason=check["reason"])
    return result if status == "all" else paginate_listing(result, limit, offset, search, status)
