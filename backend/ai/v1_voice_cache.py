"""Job-local validated speech cache; never shared between users or jobs."""
import hashlib
import json
import os
from pathlib import Path

def voice_cache_key(segment, voice_source, voice_param):
    model = Path(str(voice_param))
    fingerprint = None
    if model.is_file():
        stat = model.stat()
        fingerprint = (stat.st_size, stat.st_mtime_ns)
    raw = [3, segment.content.strip(), voice_source, str(voice_param), fingerprint,
           (segment.end-segment.start).total_seconds()]
    return hashlib.sha256(json.dumps(raw, ensure_ascii=False).encode("utf-8")).hexdigest()

def read_voice_cache(path, key):
    try:
        item=json.loads(Path(str(path)+".cache.json").read_text(encoding="utf-8"))
        stat=Path(path).stat()
        if (item["key"]==key and item["size"]==stat.st_size and
                item["mtime"]==stat.st_mtime_ns and stat.st_size>128):
            return item["duration"]
    except (OSError, ValueError, KeyError, TypeError):
        pass
    return None

def write_voice_cache(path,key,duration):
    stat=Path(path).stat()
    dest=Path(str(path)+".cache.json")
    temp=Path(str(dest)+".tmp")
    temp.write_text(json.dumps(dict(key=key,size=stat.st_size,mtime=stat.st_mtime_ns,
                                   duration=duration)),encoding="utf-8")
    os.replace(temp,dest)
