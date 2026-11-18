"""Split decoder segments at measured word boundaries before translation/TTS.

This module does not repair transcript text or interpolate timestamps. A long
decoder segment without a usable word alignment must fail instead of becoming
one fast TTS paragraph whose meaning runs ahead of the source video.
"""

from __future__ import annotations

import math
import re
from typing import Any, Sequence


class ASRSegmentationError(ValueError):
    """The transcript cannot be split without losing text or inventing timing."""


_CLAUSE_END = re.compile(r"[,，、;；:：.!?。！？…][\"'”’）)\]】]*$")
_EPSILON = 1e-6


def _compact(text: str) -> str:
    return "".join(character for character in text if not character.isspace())


def _seconds(value: Any, label: str) -> float:
    try:
        number = float(value)
    except (ValueError, TypeError) as exc:
        raise ASRSegmentationError(f"{label}: timestamp is not numeric") from exc
    if not math.isfinite(number):
        raise ASRSegmentationError(f"{label}: timestamp is not finite")
    return number


def split_aligned_asr_segments(
    raw_segments: Sequence[dict],
    *,
    max_cue_seconds: float = 6.0,
    max_cue_characters: int = 40,
    pause_seconds: float = 0.6,
    min_clause_seconds: float = 0.65,
    min_clause_characters: int = 4,
    min_cue_seconds: float = 0.7,
    max_merge_gap_seconds: float = 0.4,
) -> tuple[list[dict], dict]:
    """Return pipeline cues plus an audit mapping to untouched decoder segments.

    Clause punctuation and acoustic pauses are preferred boundaries. Duration
    and character caps also split unpunctuated speech, but only between aligned
    words. Whitespace may differ between the decoder's text and word strings;
    every other character must match, including punctuation. Output text is a
    slice of the original segment, never a reconstruction from recognized words.

    Short segments without word timestamps remain compatible with callers that
    already provide validated segment timing. They cannot be split. Word data,
    when present, is always validated even if no split is ultimately required.
    """
    if (not math.isfinite(max_cue_seconds) or max_cue_seconds <= 0
            or not isinstance(max_cue_characters, int) or max_cue_characters <= 0
            or not math.isfinite(pause_seconds) or pause_seconds <= 0
            or not math.isfinite(min_clause_seconds) or min_clause_seconds < 0
            or not isinstance(min_clause_characters, int) or min_clause_characters < 1
            or not math.isfinite(min_cue_seconds) or min_cue_seconds < 0
            or not math.isfinite(max_merge_gap_seconds) or max_merge_gap_seconds < 0):
        raise ValueError("Invalid ASR segmentation policy")

    cues: list[dict] = []
    mappings: list[dict] = []
    previous_source_end = 0.0
    for segment_index, source in enumerate(raw_segments):
        label = f"ASR segment {segment_index}"
        text = source.get("text")
        if not isinstance(text, str) or not _compact(text):
            raise ASRSegmentationError(f"{label}: empty transcript")
        start = _seconds(source.get("start"), label)
        end = _seconds(source.get("end"), label)
        if start < 0 or end <= start or start < previous_source_end - _EPSILON:
            raise ASRSegmentationError(f"{label}: invalid or overlapping source interval")
        previous_source_end = end
        compact_text = _compact(text)
        words = source.get("words")
        partitions: list[tuple[int, int, str]] = []
        character_ends: list[int] = []
        measured: list[tuple[float, float]] = []

        if not words:
            if end - start > max_cue_seconds + _EPSILON or len(compact_text) > max_cue_characters:
                raise ASRSegmentationError(f"{label}: long cue has no word alignment")
            partitions = [(0, 0, "validated_segment_without_words")]
        else:
            consumed = 0
            prior_end = start
            for word_index, word in enumerate(words):
                word_label = f"{label}, word {word_index}"
                token = word.get("word")
                if not isinstance(token, str) or not _compact(token):
                    raise ASRSegmentationError(f"{word_label}: empty aligned word")
                token_text = _compact(token)
                if compact_text[consumed:consumed + len(token_text)] != token_text:
                    raise ASRSegmentationError(f"{word_label}: word text differs from transcript")
                consumed += len(token_text)
                character_ends.append(consumed)
                word_start = _seconds(word.get("start"), word_label)
                word_end = _seconds(word.get("end"), word_label)
                if (word_start < start - _EPSILON or word_end > end + _EPSILON
                        or word_end < word_start or word_start < prior_end - _EPSILON):
                    raise ASRSegmentationError(f"{word_label}: invalid, overlapping or out-of-source alignment")
                prior_end = word_end
                measured.append((word_start, word_end))
                if word_end - word_start > max_cue_seconds + _EPSILON or len(token_text) > max_cue_characters:
                    raise ASRSegmentationError(f"{word_label}: single aligned word exceeds cue cap")
            if consumed != len(compact_text):
                raise ASRSegmentationError(f"{label}: word alignment does not cover all transcript text")

            first = 0
            index = 0
            while index < len(words):
                char_start = character_ends[first - 1] if first else 0
                duration = measured[index][1] - measured[first][0]
                character_count = character_ends[index] - char_start
                if duration > max_cue_seconds + _EPSILON or character_count > max_cue_characters:
                    if index == first or measured[index - 1][1] <= measured[first][0]:
                        raise ASRSegmentationError(f"{label}: no positive-duration word boundary within cue cap")
                    partitions.append((first, index, "cue_cap"))
                    first = index
                    continue

                final = index + 1 == len(words)
                pause = not final and measured[index + 1][0] - measured[index][1] >= pause_seconds - _EPSILON
                clause = bool(_CLAUSE_END.search(words[index]["word"].rstrip()))
                enough_clause = duration >= min_clause_seconds and character_count >= min_clause_characters
                # Never leave an all-zero-duration suffix as an independent cue.
                remainder_has_time = final or measured[-1][1] > measured[index + 1][0]
                if duration > 0 and remainder_has_time and (final or pause or clause and enough_clause):
                    reason = "segment_end" if final else "pause" if pause else "clause_end"
                    partitions.append((first, index + 1, reason))
                    first = index + 1
                index += 1
            if first < len(words):
                raise ASRSegmentationError(f"{label}: trailing text has no positive-duration alignment")

        nonspace_positions = [index for index, character in enumerate(text) if not character.isspace()]
        segment_cues = []
        for first, stop, reason in partitions:
            if not words or (len(partitions) == 1 and end - start <= max_cue_seconds + _EPSILON):
                cue_start, cue_end, content = start, end, text.strip()
                char_start, char_end = 0, len(text)
            else:
                compact_start = character_ends[first - 1] if first else 0
                compact_end = character_ends[stop - 1]
                char_start = nonspace_positions[compact_start]
                char_end = nonspace_positions[compact_end - 1] + 1
                content = text[char_start:char_end].strip()
                cue_start, cue_end = measured[first][0], measured[stop - 1][1]
            if cue_end <= cue_start or cue_end - cue_start > max_cue_seconds + _EPSILON:
                raise ASRSegmentationError(f"{label}: unsafe final cue interval")
            if cues and cue_start < cues[-1]["end"] - _EPSILON:
                raise ASRSegmentationError(f"{label}: resulting cue overlaps preceding cue")
            cue = {"id": len(cues) + 1, "start": cue_start, "end": cue_end, "text": content}
            cues.append(cue)
            segment_cues.append(cue)
            mappings.append({
                "cue_id": cue["id"], "source_segment_index": segment_index,
                "source_segment_id": source.get("id", segment_index),
                "source_word_range": [first, stop] if words else None,
                "source_character_range": [char_start, char_end],
                "source_interval": [start, end], "cue_interval": [cue_start, cue_end],
                "boundary_reason": reason,
            })
        if _compact("".join(cue["text"] for cue in segment_cues)) != compact_text:
            raise ASRSegmentationError(f"{label}: segmentation changed or dropped transcript characters")

    # A single syllable often cannot be translated/spoken naturally inside its
    # source interval. Join it to a nearby cue of the same known speaker, without
    # moving either endpoint or bridging a real pause. Any unmergeable short cue
    # remains explicit and must pass the downstream measured TTS duration gate.
    def parts(mapping: dict) -> list[dict]:
        return mapping.get("source_parts", [mapping])

    def speaker_signature(mapping: dict) -> set[tuple]:
        return {
            tuple(raw_segments[part["source_segment_index"]].get(key) for key in
                  ("speaker", "speaker_id", "role"))
            for part in parts(mapping)
        }

    def can_merge(left: int, right: int) -> bool:
        first, second = cues[left], cues[right]
        gap = second["start"] - first["end"]
        return (
            -_EPSILON <= gap <= max_merge_gap_seconds + _EPSILON
            and second["end"] - first["start"] <= max_cue_seconds + _EPSILON
            and len(_compact(first["text"] + second["text"])) <= max_cue_characters
            and speaker_signature(mappings[left]) == speaker_signature(mappings[right])
        )

    index = 0
    while index < len(cues):
        cue = cues[index]
        if cue["end"] - cue["start"] >= min_cue_seconds - _EPSILON:
            index += 1
            continue
        # Prefer the following clause: an introductory fragment belongs with the
        # action it introduces. If it is a final fragment, use the preceding cue.
        left = index if index + 1 < len(cues) and can_merge(index, index + 1) else None
        if left is None and index > 0 and can_merge(index - 1, index):
            left = index - 1
        if left is None:
            index += 1
            continue
        first, second = cues[left:left + 2]
        merged = {"id": first["id"], "start": first["start"], "end": second["end"],
                  "text": first["text"] + " " + second["text"]}
        source_parts = parts(mappings[left]) + parts(mappings[left + 1])
        merged_mapping = {
            "cue_id": merged["id"], "source_segment_index": None, "source_segment_id": None,
            "source_word_range": None, "source_character_range": None,
            "source_interval": [source_parts[0]["source_interval"][0], source_parts[-1]["source_interval"][1]],
            "cue_interval": [merged["start"], merged["end"]],
            "boundary_reason": "coalesced_short_cue", "source_parts": source_parts,
        }
        cues[left:left + 2] = [merged]
        mappings[left:left + 2] = [merged_mapping]
        index = left

    for number, (cue, mapping) in enumerate(zip(cues, mappings), 1):
        cue["id"] = number
        mapping["cue_id"] = number
    if _compact("".join(cue["text"] for cue in cues)) != _compact("".join(s["text"] for s in raw_segments)):
        raise ASRSegmentationError("Coalescing changed or dropped transcript characters")

    audit = {
        "version": 1,
        "policy": {"max_cue_seconds": max_cue_seconds, "max_cue_characters": max_cue_characters,
                   "pause_seconds": pause_seconds, "min_clause_seconds": min_clause_seconds,
                   "min_clause_characters": min_clause_characters,
                   "min_cue_seconds": min_cue_seconds, "max_merge_gap_seconds": max_merge_gap_seconds},
        "source_segment_count": len(raw_segments), "cue_count": len(cues),
        "text_preserved_ignoring_whitespace": True, "timestamps_interpolated": False,
        "unmerged_short_cue_ids": [cue["id"] for cue in cues if cue["end"] - cue["start"] < min_cue_seconds - _EPSILON],
        "mappings": mappings,
    }
    return cues, audit
