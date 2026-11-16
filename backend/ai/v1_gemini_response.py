"""Validate complete structured model output before declaring provider success."""
import json


class GeminiResponseError(ValueError):
    pass


def response_json(data, kind=list):
    try:
        candidate = data["candidates"][0]
        if candidate.get("finishReason") not in (None, "STOP"):
            raise GeminiResponseError("Incomplete or blocked Gemini response")
        raw = "".join(p.get("text", "") for p in candidate["content"]["parts"] if not p.get("thought")).strip()
        decoder = json.JSONDecoder()
        for pos, char in enumerate(raw):
            if char != ("[" if kind is list else "{"):
                continue
            try:
                value, end = decoder.raw_decode(raw[pos:])
            except ValueError:
                continue
            if isinstance(value, kind):
                return value
        raise GeminiResponseError("Missing structured JSON")
    except (KeyError, IndexError, TypeError) as exc:
        raise GeminiResponseError("Missing Gemini content") from exc


def translation_response(data, expected_count, target_lang="vi", expected_ids=None):
    values = response_json(data)
    if expected_ids is not None and values and all(isinstance(item, dict) for item in values):
        if len(values) != len(expected_ids):
            raise GeminiResponseError("Translation cue count mismatch")
        by_id = {}
        for item in values:
            raw_id = item.get("id")
            try:
                cid = int(raw_id)
            except (TypeError, ValueError) as exc:
                raise GeminiResponseError("Invalid translation cue id") from exc
            if cid in by_id:
                raise GeminiResponseError(f"Duplicate translation cue id: {cid}")
            text = item.get("text")
            if not isinstance(text, str) or not text.strip():
                raise GeminiResponseError("Empty translation cue")
            text = text.strip()
            if target_lang.lower().startswith("vi") and any("\u4e00" <= c <= "\u9fff" for c in text):
                raise GeminiResponseError("Untranslated CJK cue")
            by_id[cid] = text
        if set(by_id.keys()) != {int(eid) for eid in expected_ids}:
            raise GeminiResponseError("Translation cue id set mismatch")
        return [by_id[int(eid)] for eid in expected_ids]

    if len(values) != expected_count:
        raise GeminiResponseError("Translation cue count mismatch")
    texts = []
    for item in values:
        text = item.get("text") if isinstance(item, dict) else item
        if not isinstance(text, str) or not text.strip():
            raise GeminiResponseError("Empty translation cue")
        text = text.strip()
        if target_lang.lower().startswith("vi") and any("\u4e00" <= c <= "\u9fff" for c in text):
            raise GeminiResponseError("Untranslated CJK cue")
        texts.append(text)
    return texts
