# -*- coding: utf-8 -*-
"""
Voice Preview Service (Phase B) for Tool V1 & Tool V2.
Provides on-demand voice synthesis, preview caching, speed adjustment,
and audio serving without interfering with active video renders.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import shutil
import subprocess
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# Concurrency guard: Only 1 preview synthesis at a time to prevent resource contention
_preview_semaphore = asyncio.Semaphore(1)

# In-memory registry for preview IDs mapped to their audio paths
_ACTIVE_PREVIEWS: Dict[str, Dict[str, Any]] = {}

# Default sample texts for quick testing
SAMPLE_TEXTS = [
    {
        "id": "default",
        "category": "Mặc định",
        "text": "Xin chào, đây là giọng đọc thử nghiệm chất lượng cao trên hệ thống biên tập video tự động.",
    },
    {
        "id": "review",
        "category": "Review sản phẩm",
        "text": "Hôm nay chúng ta sẽ cùng mở hộp và đánh giá chi tiết sản phẩm này xem có thực sự đáng tiền không nhé.",
    },
    {
        "id": "tutorial",
        "category": "Video hướng dẫn",
        "text": "Các thao tác thực hiện rất đơn giản, bạn chỉ cần làm theo từng bước hướng dẫn cụ thể trên màn hình.",
    },
    {
        "id": "story",
        "category": "Kể chuyện / Tâm sự",
        "text": "Có những buổi chiều tĩnh lặng, khi một tách trà thơm cũng đủ làm ta thấy lòng nhẹ nhõm đến lạ kỳ.",
    },
]

# Fallback catalog if voice_catalog.json is not found
FALLBACK_CATALOG = [
    {"id": "microsoft-hoaimy", "label": "Microsoft · Hoài My (nữ)", "source": "edge", "param": "vi-VN-HoaiMyNeural"},
    {"id": "microsoft-namminh", "label": "Microsoft · Nam Minh (nam)", "source": "edge", "param": "vi-VN-NamMinhNeural"},
    {"id": "chi-mai", "label": "Chí Mai · RVC (mặc định)", "source": "rvc", "param": ""},
    {"id": "capcut-BV421_vivn_streaming", "label": "CapCut · Nhỏ Ngọt Ngào", "source": "capcut", "param": "BV421_vivn_streaming"},
    {"id": "capcut-vi_female_huong", "label": "CapCut · Giọng Nữ Phổ Thông", "source": "capcut", "param": "vi_female_huong"},
    {"id": "capcut-BV074_streaming", "label": "CapCut · Cô Gái Hoạt Ngôn", "source": "capcut", "param": "BV074_streaming"},
    {"id": "capcut-BV562_streaming", "label": "CapCut · Mai", "source": "capcut", "param": "BV562_streaming"},
    {"id": "capcut-BV007_streaming", "label": "CapCut · Nam Phổ Thông", "source": "capcut", "param": "BV007_streaming"},
    {"id": "capcut-BV008_streaming", "label": "CapCut · Nam Trầm Ấm", "source": "capcut", "param": "BV008_streaming"},
]


def resolve_workspace_dir() -> Path:
    ws_env = os.getenv("AUTODUB_WORKSPACE")
    if ws_env and Path(ws_env).is_dir():
        return Path(ws_env)
    for c in [
        Path(r"C:\tool v1\workspace"),
        Path(r"C:\tool v2\workspace"),
        Path(__file__).resolve().parent.parent / "workspace",
    ]:
        if c.is_dir():
            return c
    # Fallback to local workspace in backend parent
    fallback = Path(__file__).resolve().parent.parent / "workspace"
    fallback.mkdir(parents=True, exist_ok=True)
    return fallback


def get_cache_dir() -> Path:
    ws = resolve_workspace_dir()
    d = ws / "voice_previews_cache"
    d.mkdir(parents=True, exist_ok=True)
    return d


def get_cache_index_path() -> Path:
    ws = resolve_workspace_dir()
    control = ws / "control"
    control.mkdir(parents=True, exist_ok=True)
    return control / "voice_previews_cache.json"


def get_voice_catalog() -> List[Dict[str, Any]]:
    ws = resolve_workspace_dir()
    candidates = [
        ws / "bot_system" / "control" / "voice_catalog.json",
        ws / "control" / "voice_catalog.json",
        Path(r"C:\tool v1\workspace\bot_system\control\voice_catalog.json"),
        Path(r"C:\tool v1\workspace\control\voice_catalog.json"),
    ]
    for p in candidates:
        if p.is_file():
            try:
                with open(p, "r", encoding="utf-8-sig") as f:
                    return json.load(f)
            except Exception as e:
                logger.warning("Lỗi đọc voice_catalog.json từ %s: %s", p, e)
    return FALLBACK_CATALOG


def resolve_verified_sample(voice_id: str) -> Optional[Path]:
    ws = resolve_workspace_dir()
    candidates = [
        ws / "bot_system" / "control" / "voice_checks",
        ws / "control" / "voice_checks",
        Path(r"C:\tool v1\workspace\bot_system\control\voice_checks"),
        Path(r"C:\tool v1\workspace\control\voice_checks"),
    ]
    for folder in candidates:
        if not folder.is_dir():
            continue
        for ext in [".mp3", ".wav"]:
            sample_file = folder / f"{voice_id}{ext}"
            if sample_file.is_file() and sample_file.stat().st_size > 128:
                return sample_file
    return None


def get_audio_duration_seconds(file_path: Path) -> float:
    try:
        cmd = [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            str(file_path),
        ]
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=5)
        if res.returncode == 0 and res.stdout.strip():
            return round(float(res.stdout.strip()), 2)
    except Exception:
        pass
    # Rough estimate if ffprobe fails: 16KB/sec for 128kbps mp3
    try:
        return round(file_path.stat().st_size / 16000.0, 2)
    except Exception:
        return 0.0


def _load_cache_index() -> Dict[str, Any]:
    p = get_cache_index_path()
    if p.is_file():
        try:
            with open(p, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}
    return {}


def _save_cache_index(index_data: Dict[str, Any]) -> None:
    p = get_cache_index_path()
    tmp = p.with_suffix(".tmp")
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(index_data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, p)
    except Exception as e:
        logger.error("Lỗi lưu voice_previews_cache.json: %s", e)


def clean_cache(max_size_mb: int = 50, ttl_days: int = 7) -> None:
    """Enforces LRU and TTL limits on cached preview audio files."""
    cache_dir = get_cache_dir()
    index_data = _load_cache_index()
    now = time.time()
    ttl_seconds = ttl_days * 86400
    max_bytes = max_size_mb * 1024 * 1024

    entries = list(index_data.items())
    # 1. Purge expired entries
    retained = {}
    for k, v in entries:
        created = v.get("created_at", now)
        f_path = Path(v.get("file_path", ""))
        if (now - created) > ttl_seconds:
            if f_path.is_file():
                try:
                    f_path.unlink()
                except OSError:
                    pass
        else:
            if f_path.is_file():
                retained[k] = v

    # 2. LRU eviction if total size exceeds max_bytes
    total_size = sum(Path(v["file_path"]).stat().st_size for v in retained.values() if Path(v.get("file_path", "")).is_file())
    if total_size > max_bytes:
        # Sort by last_accessed ascending
        sorted_entries = sorted(retained.items(), key=lambda x: x[1].get("last_accessed", 0))
        for k, v in sorted_entries:
            f_path = Path(v.get("file_path", ""))
            if f_path.is_file():
                try:
                    size = f_path.stat().st_size
                    f_path.unlink()
                    total_size -= size
                except OSError:
                    pass
            retained.pop(k, None)
            if total_size <= max_bytes * 0.8:
                break

    _save_cache_index(retained)


def compute_preview_hash(voice_id: str, speed: float, text: str) -> str:
    key_str = f"{voice_id.strip()}::{speed:.2f}::{text.strip()}"
    return hashlib.sha256(key_str.encode("utf-8")).hexdigest()[:24]


async def synthesize_voice_preview(
    voice_id: str,
    text: str = "",
    speed: float = 1.0,
    pitch: str = "+0Hz",
) -> Dict[str, Any]:
    """
    Synthesizes or retrieves cached audio preview for given voice, speed, and text.
    Uses asyncio.Semaphore(1) to avoid overloading CPU/GPU during active renders.
    """
    # 1. Clean input
    text = (text or "").strip()
    if not text:
        text = SAMPLE_TEXTS[0]["text"]
    if len(text) > 350:
        text = text[:350] + "..."

    speed = max(0.5, min(float(speed), 2.0))

    # 2. Resolve voice catalog info
    cat = get_voice_catalog()
    voice_info = next((v for v in cat if v["id"] == voice_id), None)
    if not voice_info:
        # Fallback search
        voice_info = next((v for v in FALLBACK_CATALOG if v["id"] == voice_id), None)
    if not voice_info:
        voice_info = {
            "id": voice_id,
            "label": voice_id,
            "source": "edge" if "hoaimy" in voice_id or "namminh" in voice_id else "capcut",
            "param": "vi-VN-HoaiMyNeural" if "hoaimy" in voice_id else "vi-VN-NamMinhNeural",
        }

    voice_label = voice_info.get("label", voice_id)
    source = voice_info.get("source", "edge")
    param = voice_info.get("param", "")

    cache_hash = compute_preview_hash(voice_id, speed, text)
    cache_dir = get_cache_dir()
    cached_file = cache_dir / f"{cache_hash}.mp3"
    preview_id = f"pv_{cache_hash[:16]}"

    index_data = _load_cache_index()

    # 3. Check Cache
    if cached_file.is_file() and cached_file.stat().st_size > 128:
        duration = get_audio_duration_seconds(cached_file)
        # Update last accessed
        if cache_hash in index_data:
            index_data[cache_hash]["last_accessed"] = time.time()
            _save_cache_index(index_data)
        res = {
            "status": "ready",
            "preview_id": preview_id,
            "cached": True,
            "audio_url": f"/api/voice-preview/{preview_id}/audio",
            "duration": duration,
            "voice_id": voice_id,
            "voice_label": voice_label,
            "provider": source,
            "speed": speed,
            "text": text,
        }
        _ACTIVE_PREVIEWS[preview_id] = {
            "file_path": str(cached_file),
            "meta": res,
        }
        return res

    # 4. Synthesize with concurrency guard
    async with _preview_semaphore:
        # Re-check cache inside semaphore
        if cached_file.is_file() and cached_file.stat().st_size > 128:
            duration = get_audio_duration_seconds(cached_file)
            res = {
                "status": "ready",
                "preview_id": preview_id,
                "cached": True,
                "audio_url": f"/api/voice-preview/{preview_id}/audio",
                "duration": duration,
                "voice_id": voice_id,
                "voice_label": voice_label,
                "provider": source,
                "speed": speed,
                "text": text,
            }
            _ACTIVE_PREVIEWS[preview_id] = {"file_path": str(cached_file), "meta": res}
            return res

        temp_raw = cache_dir / f"temp_{uuid.uuid4().hex}.mp3"
        warning = None

        try:
            # Case A: If user requested default sample text, 1.0x speed, and verified sample exists
            verified_sample = resolve_verified_sample(voice_id)
            is_default_text = text in [s["text"] for s in SAMPLE_TEXTS]
            if is_default_text and speed == 1.0 and verified_sample and verified_sample.is_file():
                shutil.copy2(verified_sample, cached_file)
            else:
                # Need fresh synthesis
                synthesized = False

                if source == "edge":
                    import edge_tts
                    edge_voice = param or ("vi-VN-NamMinhNeural" if "namminh" in voice_id else "vi-VN-HoaiMyNeural")
                    for attempt in range(3):
                        try:
                            if temp_raw.is_file():
                                temp_raw.unlink()
                            communicate = edge_tts.Communicate(text, edge_voice)
                            await asyncio.wait_for(communicate.save(str(temp_raw)), timeout=15.0)
                            if temp_raw.is_file() and temp_raw.stat().st_size > 128:
                                synthesized = True
                                break
                        except Exception as _edge_err:
                            logger.warning("Edge TTS attempt %d failed: %s", attempt + 1, _edge_err)
                            await asyncio.sleep(0.5)

                    if not synthesized and verified_sample and verified_sample.is_file():
                        shutil.copy2(verified_sample, temp_raw)
                        synthesized = True
                        warning = "Dịch vụ Edge TTS tạm thời gián đoạn; đang phát âm thanh mẫu kiểm định."

                elif source == "capcut":
                    try:
                        from ai.voice_cloning import _run_capcut_tts
                        capcut_voice = param or "BV562_streaming"
                        await asyncio.to_thread(_run_capcut_tts, text, str(temp_raw), capcut_voice)
                        synthesized = temp_raw.is_file() and temp_raw.stat().st_size > 128
                    except Exception as e:
                        logger.warning("CapCut TTS thất bại (%s), thử fallback sample hoặc Edge TTS: %s", voice_id, e)
                        if verified_sample and verified_sample.is_file():
                            shutil.copy2(verified_sample, temp_raw)
                            synthesized = True
                            warning = "CapCut tạm thời không phản hồi, đang phát mẫu kiểm định có sẵn."
                        else:
                            # Fallback to Edge HoaiMy
                            import edge_tts
                            communicate = edge_tts.Communicate(text, "vi-VN-HoaiMyNeural", rate="+0%")
                            await asyncio.wait_for(communicate.save(str(temp_raw)), timeout=15.0)
                            synthesized = temp_raw.is_file() and temp_raw.stat().st_size > 128
                            warning = "CapCut API chưa sẵn sàng; đã tạm thay thế bằng giọng Hoài My."

                elif source == "rvc" or voice_id == "chi-mai":
                    # For RVC/Chí Mai preview, use the verified audio sample
                    if verified_sample and verified_sample.is_file():
                        shutil.copy2(verified_sample, temp_raw)
                        synthesized = True
                    else:
                        import edge_tts
                        communicate = edge_tts.Communicate(text, "vi-VN-HoaiMyNeural", rate="+0%")
                        await asyncio.wait_for(communicate.save(str(temp_raw)), timeout=15.0)
                        synthesized = temp_raw.is_file() and temp_raw.stat().st_size > 128
                        warning = "Chưa có file mẫu Chí Mai RVC, đang phát mẫu tham chiếu Hoài My."

                if not synthesized or not temp_raw.is_file() or temp_raw.stat().st_size < 128:
                    raise RuntimeError("Không thể tạo âm thanh xem trước từ provider.")

                # If speed is not 1.0, adjust using ffmpeg atempo
                if abs(speed - 1.0) > 0.03:
                    atempo_filter = f"atempo={speed:.2f}"
                    cmd = ["ffmpeg", "-y", "-i", str(temp_raw), "-filter:a", atempo_filter, str(cached_file)]
                    p = await asyncio.to_thread(subprocess.run, cmd, capture_output=True, text=True)
                    if p.returncode != 0 or not cached_file.is_file():
                        # If ffmpeg failed, fallback to raw
                        shutil.copy2(temp_raw, cached_file)
                else:
                    shutil.copy2(temp_raw, cached_file)

        finally:
            if temp_raw.is_file():
                try:
                    temp_raw.unlink()
                except OSError:
                    pass

        duration = get_audio_duration_seconds(cached_file)

        # Save to index
        now_ts = time.time()
        index_data[cache_hash] = {
            "preview_id": preview_id,
            "voice_id": voice_id,
            "speed": speed,
            "text": text,
            "file_path": str(cached_file),
            "created_at": now_ts,
            "last_accessed": now_ts,
            "duration": duration,
        }
        _save_cache_index(index_data)

        # Cleanup if necessary (async background or inline)
        clean_cache(max_size_mb=50, ttl_days=7)

        res = {
            "status": "ready",
            "preview_id": preview_id,
            "cached": False,
            "audio_url": f"/api/voice-preview/{preview_id}/audio",
            "duration": duration,
            "voice_id": voice_id,
            "voice_label": voice_label,
            "provider": source,
            "speed": speed,
            "text": text,
        }
        if warning:
            res["warning"] = warning

        _ACTIVE_PREVIEWS[preview_id] = {"file_path": str(cached_file), "meta": res}
        return res


def get_preview_audio_path(preview_id: str) -> Optional[Path]:
    """Retrieves file path for an active or cached preview ID."""
    if preview_id in _ACTIVE_PREVIEWS:
        p = Path(_ACTIVE_PREVIEWS[preview_id]["file_path"])
        if p.is_file():
            return p

    # Look up in cache index
    index_data = _load_cache_index()
    for item in index_data.values():
        if item.get("preview_id") == preview_id:
            p = Path(item.get("file_path", ""))
            if p.is_file():
                return p

    # Look up directly in cache dir
    cache_dir = get_cache_dir()
    for f in cache_dir.glob("*.mp3"):
        if preview_id.replace("pv_", "") in f.stem:
            return f

    return None
