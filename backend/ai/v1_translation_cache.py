"""Validated translation cache, keyed by the complete prompt and image context."""
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile


MOJIBAKE_PATTERNS = re.compile(
    r'[\xc2-\xdf][\x80-\xbf\u20ac\u201a\u0192\u201e\u2026\u2020\u2021\u02c6\u2030\u0160\u2039\u0152\u017d\u2018\u2019\u201c\u201d\u2022\u2013\u2014\u02dc\u2122\u0161\u203a\u0153\u017e\u0178]|'
    r'[\xe0-\xef][\x80-\xbf\u20ac\u201a\u0192\u201e\u2026\u2020\u2021\u02c6\u2030\u0160\u2039\u0152\u017d\u2018\u2019\u201c\u201d\u2022\u2013\u2014\u02dc\u2122\u0161\u203a\u0153\u017e\u0178]{2}|'
    r'Ã¢|Ã¡|Ã|Ä‘|Ä|áº|á»|á»™|cÅ|dÃ|báº¡ng|vá» e'
)


def is_mojibake(text: str) -> bool:
    if not isinstance(text, str):
        return False
    return bool(MOJIBAKE_PATTERNS.search(text))


def cache_key(parts, models, account):
    data = json.dumps([1, parts, models, account], ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(data.encode("utf-8")).hexdigest()


def cache_root():
    return Path(os.getenv("AUTODUB_WORKSPACE", str(Path(__file__).resolve().parents[2] / "workspace"))) / "translation_cache"


def read_cache(key, count, target_lang):
    cache_file = cache_root() / (key + ".json")
    try:
        data = json.loads(cache_file.read_text(encoding="utf-8"))
        texts = data["texts"]
        if not isinstance(texts, list) or len(texts) != count:
            return None
        if not all(isinstance(t, str) and t.strip() for t in texts):
            return None
        if target_lang.lower().startswith("vi"):
            if any("\u4e00" <= c <= "\u9fff" for t in texts for c in t):
                return None
            if any(is_mojibake(t) for t in texts):
                try:
                    cache_file.unlink(missing_ok=True)
                except OSError:
                    pass
                return None
        if not isinstance(data.get("model"), str):
            return None
        return data
    except (OSError, ValueError, TypeError, KeyError):
        return None


def write_cache(key, texts, model):
    if not isinstance(texts, list) or any(is_mojibake(t) for t in texts):
        return
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
