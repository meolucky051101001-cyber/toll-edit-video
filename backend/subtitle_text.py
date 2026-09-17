"""Text cleanup and sentence boundaries shared by translation and subtitles."""

import re
import unicodedata
from copy import copy


def normalize_subtitle_text(text):
    text = unicodedata.normalize("NFC", str(text or ""))
    # Remove pauses written as ..., . . . or a Unicode ellipsis without joining
    # the words on either side. A single sentence-ending period is preserved.
    # Handle all combinations: ..., …., .…, ‥, ⋯, etc. without leaving stray dots.
    text = re.sub(
        r"(?:[.\u2025\u2026\u22ef\ufe19\u22ee][ \t]*){2,}|[\u2025\u2026\u22ef\ufe19\u22ee]+",
        " ",
        text,
    )
    text = re.sub(r"\s+", " ", text.replace(r"\N", " ")).strip()
    text = re.sub(r"\s+([,.!?;:])", r"\1", text)
    # Strip terminal dots from incomplete clauses ending in conjunctions/prepositions
    text = re.sub(
        r"(?i)\b(và|hoặc|hay|nhưng|mà|thì|là|của|với|về|cho|vì|như|rằng|đang|sẽ|đã|được|bị|bởi|tại|nếu|dù|tuy|khi|lúc|để|do)\s*\.+$",
        r"\1",
        text,
    )
    return text


ABBREVIATIONS = {
    "tp", "q", "p", "ts", "ths", "pgs", "gs", "mr", "mrs", "ms", "dr", "prof",
    "st", "vs", "co", "ltd", "corp", "inc", "etc", "no", "vol", "vv", "v.v",
    "tt", "tx", "bt", "ttg",
}

CONTINUING_WORDS_RE = re.compile(
    r"^(?:thì|nên|cho nên|mà|nhưng|hoặc|hay|và|lại|cũng|bèn|vẫn|cứ)\b",
    re.IGNORECASE,
)


def clean_incomplete_segment_stops(items):
    """Normalize sentence endings across contiguous segments to prevent premature terminal periods
    on incomplete clauses (câu lửng).

    Accepts either a sequence of strings or a sequence of objects with a `content` attribute.
    Returns the modified sequence (or updates objects in place).
    """
    if not items:
        return items
    is_obj = hasattr(items[0], "content")
    texts = [str(getattr(it, "content", it) or "") for it in items]
    n = len(texts)
    for i in range(n - 1):
        curr = texts[i]
        nxt = texts[i + 1]
        if not curr or not nxt:
            continue
        if curr.endswith("."):
            nxt_stripped = nxt.strip()
            if not nxt_stripped:
                continue
            first_char = nxt_stripped[0]
            is_lowercase = first_char.isalpha() and first_char.islower()
            is_continuing_connector = bool(CONTINUING_WORDS_RE.match(nxt_stripped))
            if is_lowercase or is_continuing_connector:
                curr = curr.rstrip(".").rstrip()
                texts[i] = curr
                if is_obj:
                    items[i].content = curr
    return items if is_obj else texts


def split_subtitle_sentences(text):
    text = normalize_subtitle_text(text)
    sentences = []
    start = 0
    # Periods inside decimals, version numbers, domains and dotted names are
    # not sentence endings. Closing quotation marks stay with their sentence.
    for match in re.finditer(r"[.!?。！？]+[\"'”’»)]*(?=\s|$)", text):
        if match.group().startswith("."):
            token = text[:match.start()].rsplit(" ", 1)[-1].casefold().rstrip(".")
            if token in ABBREVIATIONS:
                continue
        sentence = text[start:match.end()].strip()
        if any(char.isalnum() for char in sentence):
            sentences.append(sentence)
        start = match.end()
    remaining = text[start:].strip()
    if any(char.isalnum() for char in remaining):
        sentences.append(remaining)
    return sentences


def split_segment_text(segment, parts):
    """Partition one time window without gaps or changing its OCR source ID."""
    parts = [part for part in parts if any(char.isalnum() for char in part)]
    if not parts or segment.end <= segment.start:
        return []
    # Vietnamese spoken words are syllables, so word count is a better timing
    # estimate than splitting a sentence into equal-duration visual chunks.
    weights = [max(len(part.split()), 1) for part in parts]
    total_weight = sum(weights)
    consumed = 0
    current = segment.start
    result = []
    for position, (part, weight) in enumerate(zip(parts, weights)):
        consumed += weight
        end = (
            segment.end if position == len(parts) - 1 else
            segment.start + (segment.end - segment.start) * (consumed / total_weight)
        )
        item = copy(segment)
        item.content = part
        item.start, item.end = current, end
        item.source_segment_id = getattr(segment, "source_segment_id", None) or segment.index
        result.append(item)
        current = end
    return result
