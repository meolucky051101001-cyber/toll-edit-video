"""Validated translation cache, keyed by the complete prompt and image context."""
import hashlib
import json
import os
from pathlib import Path
import tempfile


def cache_key(parts, models, account):
    data = json.dumps([1, parts, models, account], ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(data.encode("utf-8")).hexdigest()


def cache_root():
    return Path(os.getenv("AUTODUB_WORKSPACE", str(Path(__file__).resolve().parents[2] / "workspace"))) / "translation_cache"


def read_cache(key, count, target_lang):
    try:
        data = json.loads((cache_root() / (key + ".json")).read_text(encoding="utf-8"))
        texts = data["texts"]
        if not isinstance(texts, list) or len(texts) != count:
            return None
        if not all(isinstance(t, str) and t.strip() for t in texts):
            return None
        if target_lang.lower().startswith("vi") and any("\u4e00" <= c <= "\u9fff" for t in texts for c in t):
            return None
        if not isinstance(data.get("model"), str):
            return None
        return data
    except (OSError, ValueError, TypeError, KeyError):
        return None


def write_cache(key, texts, model):
    temporary = None
    try:
        root = cache_root()
        root.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=root, suffix=".tmp", delete=False) as stream:
            temporary = stream.name
            json.dump({"texts": texts, "model": model}, stream, ensure_ascii=False)
        os.replace(temporary, root / (key + ".json"))
    except OSError:
        pass  # A cache failure must not fail a valid translation.
    finally:
        if temporary:
            try:
                os.unlink(temporary)
            except OSError:
                pass
