"""Text cleanup and sentence boundaries shared by translation and subtitles."""

import re
import unicodedata
from copy import copy


def normalize_subtitle_text(text):
    text = unicodedata.normalize("NFC", str(text or ""))
    # Remove pauses written as ..., . . . or a Unicode ellipsis without joining
    # the words on either side. A single sentence-ending period is preserved.
    text = re.sub(r"(?:\.[ \t]*){2,}|[…⋯]+", " ", text)
    text = re.sub(r"\s+", " ", text.replace(r"\N", " ")).strip()
    return re.sub(r"\s+([,.!?;:])", r"\1", text)


def split_subtitle_sentences(text):
    text = normalize_subtitle_text(text)
    sentences = []
    start = 0
    # Periods inside decimals, version numbers, domains and dotted names are
    # not sentence endings. Closing quotation marks stay with their sentence.
    for match in re.finditer(r"[.!?。！？]+[\"'”’»)]*(?=\s|$)", text):
        if match.group().startswith("."):
            token = text[:match.start()].rsplit(" ", 1)[-1].casefold()
            if token in {"tp", "q", "p", "ts", "ths", "pgs", "gs", "mr", "mrs", "dr"}:
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
