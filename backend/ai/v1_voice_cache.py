"""Global validated speech cache shared across all jobs to save API quota."""
import hashlib
import json
import os
import shutil
from pathlib import Path

GLOBAL_CACHE_DIR = Path(__file__).parent.parent / "voice_cache"
GLOBAL_CACHE_DIR.mkdir(parents=True, exist_ok=True)

CACHE_KEY_VERSION = 5  # Version 5: Invalidate legacy v4 cache to prevent Edge TTS rescue leakages

def voice_cache_key(segment, voice_source, voice_param):
    model = Path(str(voice_param))
    fingerprint = None
    if model.is_file():
        stat = model.stat()
        fingerprint = (stat.st_size, stat.st_mtime_ns)
    # Round duration to 1 decimal place to increase cache hit rate for identical texts
    target_dur = round((segment.end - segment.start).total_seconds(), 1)
    raw = [CACHE_KEY_VERSION, segment.content.strip(), voice_source, str(voice_param), fingerprint, target_dur]
    return hashlib.sha256(json.dumps(raw, ensure_ascii=False).encode("utf-8")).hexdigest()

def is_valid_audio(audio_path: str | Path, min_duration: float = 0.15) -> bool:
    """Kiem tra audio thuc te: co the giai ma va co thoi luong hop le (Codex Requirement)."""
    p = Path(audio_path)
    if not p.is_file() or p.stat().st_size < 256:
        return False
    try:
        from pydub import AudioSegment
        seg = AudioSegment.from_file(str(p))
        dur_s = len(seg) / 1000.0
        return dur_s >= min_duration
    except Exception:
        pass
    try:
        import soundfile as sf
        info = sf.info(str(p))
        return info.duration >= min_duration
    except Exception:
        return False

def read_voice_cache(path, key, text_content=""):
    # 1. Check job-local cache
    try:
        item = json.loads(Path(str(path)+".cache.json").read_text(encoding="utf-8"))
        stat = Path(path).stat()
        if (item["key"]==key and item["size"]==stat.st_size and
                item["mtime"]==stat.st_mtime_ns and stat.st_size>128):
            if is_valid_audio(path):
                return item["duration"]
    except (OSError, ValueError, KeyError, TypeError):
        pass
        
    # 2. Check global cache
    global_audio = GLOBAL_CACHE_DIR / f"{key}.mp3"
    global_meta = GLOBAL_CACHE_DIR / f"{key}.json"
    
    try:
        if global_audio.exists() and global_meta.exists():
            item = json.loads(global_meta.read_text(encoding="utf-8"))
            if global_audio.stat().st_size > 128 and is_valid_audio(global_audio):
                # Global Cache HIT
                shutil.copy2(global_audio, path)
                write_voice_cache(path, key, item["duration"], text_content, skip_global=True)
                short_txt = text_content[:40].replace('\n', ' ')
                print(f"✅ [CACHE HIT] Tái sử dụng audio: '{short_txt}...'")
                return item["duration"]
    except Exception:
        pass
        
    return None

def write_voice_cache(path, key, duration, text_content="", skip_global=False):
    # Chi ghi cache neu audio hop le va giai ma duoc
    if not is_valid_audio(path):
        return

    # Job local
    stat = Path(path).stat()
    dest = Path(str(path)+".cache.json")
    temp = Path(str(dest)+".tmp")
    temp.write_text(json.dumps(dict(key=key, size=stat.st_size, mtime=stat.st_mtime_ns,
                                   duration=duration)), encoding="utf-8")
    os.replace(temp, dest)
    
    # Global
    if not skip_global:
        try:
            global_audio = GLOBAL_CACHE_DIR / f"{key}.mp3"
            global_meta = GLOBAL_CACHE_DIR / f"{key}.json"
            if not global_audio.exists() or global_audio.stat().st_size == 0:
                shutil.copy2(path, global_audio)
                global_meta.write_text(json.dumps(dict(duration=duration, text=text_content)), encoding="utf-8")
        except Exception:
            pass
