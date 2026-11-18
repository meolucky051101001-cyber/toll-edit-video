"""Validate small ASR substitutions against repeated, frame-bound caption reads.

The multimodal response is untrusted evidence, not an instruction. This module
does not invoke a model, change timestamps, insert text, or alter the input cue.
The caller supplies the timestamps of frames actually sent to the reader.
"""
from __future__ import annotations

from difflib import SequenceMatcher
import math
import re
import unicodedata
from typing import Any, Mapping, Sequence


def _is_cjk(char: str) -> bool:
    return "\u3400" <= char <= "\u4dbf" or "\u4e00" <= char <= "\u9fff"


def _content_chars(text: str) -> list[tuple[int, str]]:
    # Keep the original offsets so accepted output preserves punctuation and
    # spacing exactly. No normalization may rewrite a number or Latin token.
    return [(i, char) for i, char in enumerate(text)
            if not char.isspace() and not unicodedata.category(char).startswith("P")]


def derive_caption_substitution(
    segment: Mapping[str, Any], proposal: Mapping[str, Any], *,
    sampled_times: Sequence[float], min_sample_span: float = 0.15,
) -> dict[str, Any] | None:
    """Derive a small substitution from repeated caption reads, or return None.

    Ignore the model's proposed rewrite and audio transcript. Each frame must
    independently derive the same equal-length CJK replacements, after which
    the usual caller-bound, local-context validator checks all supplied reads.
    Source prefixes, suffixes, punctuation, numbers and Latin tokens survive.
    Caption text outside the source's aligned span cannot extend the cue.
    """
    if not isinstance(segment, Mapping) or not isinstance(proposal, Mapping):
        return None
    original = segment.get("text")
    reads = proposal.get("caption_reads")
    if (not isinstance(original, str) or not original or len(original) > 1000
            or not isinstance(reads, list) or not 2 <= len(reads) <= 8):
        return None
    source_map = [(i, char) for i, char in enumerate(original) if _is_cjk(char)]
    source = "".join(char for _, char in source_map)
    if not source:
        return None
    candidates: dict[str, list[int]] = {}
    for read_index, read in enumerate(reads):
        if (not isinstance(read, Mapping) or not isinstance(read.get("text"), str)
                or not read["text"].strip() or len(read["text"]) > 2000):
            return None
        caption = "".join(char for char in read["text"] if _is_cjk(char))
        opcodes = SequenceMatcher(None, source, caption, autojunk=False).get_opcodes()
        characters = list(original)
        changed = False
        for opcode_index, (tag, a1, a2, b1, b2) in enumerate(opcodes):
            if tag != "replace":
                continue
            source_len, caption_len = a2 - a1, b2 - b1
            if source_len == caption_len:
                source_base, source_slice, caption_slice = a1, source[a1:a2], caption[b1:b2]
            else:
                # A caption can include a neighbouring clause. Salvage only an
                # edge replacement when one side of this opcode is anchored by
                # an equal block and the other side is the cue boundary. With
                # equal anchors on both sides the unequal edit is insertion or
                # deletion inside the cue and must be rejected.
                left_anchor = any(row[0] == "equal" and row[2] > row[1]
                                  for row in opcodes[:opcode_index])
                right_anchor = any(row[0] == "equal" and row[2] > row[1]
                                   for row in opcodes[opcode_index + 1:])
                if left_anchor and right_anchor:
                    continue
                if source_len < caption_len:
                    width = source_len
                    if width == 0:
                        continue
                    if right_anchor:
                        source_base, source_slice = a1, source[a1:a2]
                        caption_slice = caption[b2 - width:b2]
                    else:
                        source_base, source_slice = a1, source[a1:a2]
                        caption_slice = caption[b1:b1 + width]
                else:
                    width = caption_len
                    if width == 0:
                        continue
                    if right_anchor:
                        source_base, source_slice = a2 - width, source[a2 - width:a2]
                        caption_slice = caption[b1:b2]
                    else:
                        source_base, source_slice = a1, source[a1:a1 + width]
                        caption_slice = caption[b1:b2]
            # After edge bounding, still accept only equal-length replace
            # opcodes. Repeated glyphs can otherwise conceal insert/delete.
            for local_tag, s1, s2, c1, c2 in SequenceMatcher(
                    None, source_slice, caption_slice, autojunk=False).get_opcodes():
                if local_tag != "replace" or s2 - s1 != c2 - c1:
                    continue
                for source_offset, replacement in zip(range(s1, s2), caption_slice[c1:c2]):
                    index = source_base + source_offset
                    characters[source_map[index][0]] = replacement
                    changed = True
        if changed:
            candidate = "".join(characters)
            candidates.setdefault(candidate, []).append(read_index)

    # Ambiguous full candidates cannot be selected by majority or by the model's
    # corrected_text. The validator below also rejects conflicting local reads.
    if len(candidates) != 1:
        return None
    candidate, supporting_reads = next(iter(candidates.items()))
    if len(supporting_reads) < 2:
        return None
    candidate_proposal = dict(proposal, corrected_text=candidate)
    verdict = validate_visual_caption_correction(
        segment, candidate_proposal, sampled_times=sampled_times,
        min_sample_span=min_sample_span,
    )
    if not verdict["accepted"]:
        return None
    verdict["audit"].update(
        method="frame_bound_equal_length_substitution_from_caption_reads_v1",
        supporting_read_count=len(supporting_reads),
        supporting_read_indices=supporting_reads,
    )
    return verdict


def validate_visual_caption_correction(
    segment: Mapping[str, Any], proposal: Mapping[str, Any], *,
    sampled_times: Sequence[float], min_sample_span: float = 0.15,
    allow_audio_corroboration: bool = False, audio_sha256: str | None = None,
) -> dict[str, Any]:
    """Return ``{accepted, text, audit}``; rejected output always retains source.

    Expected proposal: ``{id, corrected_text, caption_reads:[{sample_time,text}]}``.
    Only equal-length CJK substitutions are allowed (up to two, or three for
    sources with at least ten CJK characters; always at most 40%). Each changed
    position needs at least two unchanged CJK neighbours in a contiguous local
    context and two independent caption frames corroborating that context.
    Captions may contain additional text, but their local evidence must agree.
    ``sampled_times`` must come from the caller's extracted frames, never from
    the model response. This verifies consistency, not the reader's truthfulness.
    An explicit audio-assisted mode permits larger substitutions only in short
    3–6 CJK cues, with a caller-bound audio hash, identical full audio transcript,
    and at least two identical full caption reads. It is a separate recorded
    review policy, not independent model agreement or a relaxed default.
    """
    original = segment.get("text", "") if isinstance(segment, Mapping) else ""
    original = original if isinstance(original, str) else ""
    audit: dict[str, Any] = {"method": "frame_bound_cjk_substitution_v1", "reason": "",
                             "substitutions": [], "frames_bound_to_caller": False}

    def reject(reason: str) -> dict[str, Any]:
        audit["reason"] = reason
        return {"accepted": False, "text": original, "audit": audit}

    if not isinstance(segment, Mapping) or not isinstance(proposal, Mapping):
        return reject("invalid_schema")
    sid, pid = segment.get("id"), proposal.get("id")
    if sid is None or isinstance(sid, bool) or isinstance(pid, bool) or str(sid) != str(pid):
        return reject("cue_id_mismatch")
    try:
        start, end = float(segment["start"]), float(segment["end"])
        trusted_times = [float(t) for t in sampled_times]
    except (KeyError, ValueError, TypeError, OverflowError):
        return reject("invalid_frame_times")
    if (not math.isfinite(start) or not math.isfinite(end) or not 0 <= start < end
            or not math.isfinite(min_sample_span) or min_sample_span < 0.15
            or not trusted_times or any(not math.isfinite(t) or t < start or t > end for t in trusted_times)):
        return reject("invalid_frame_times")
    audit["frames_bound_to_caller"] = True
    proposed = proposal.get("corrected_text")
    if not original or not isinstance(proposed, str) or len(original) > 1000 or len(proposed) > 1000:
        return reject("invalid_text")
    src_map, dst_map = _content_chars(original), _content_chars(proposed)
    src = "".join(char for _, char in src_map)
    dst = "".join(char for _, char in dst_map)
    if len(src) != len(dst):
        return reject("character_insertion_or_deletion")
    changed = [i for i, (left, right) in enumerate(zip(src, dst)) if left != right]
    if not changed:
        return reject("no_cjk_substitution")
    if any(not _is_cjk(src[i]) or not _is_cjk(dst[i]) for i in changed):
        return reject("non_cjk_change")
    cjk_count = sum(_is_cjk(char) for char in src)
    exceeds_default = len(changed) > (3 if cjk_count >= 10 else 2) or len(changed) / max(cjk_count, 1) > 0.4
    audio_text = proposal.get("audio_transcript")
    audio_compact = "".join(char for _, char in _content_chars(audio_text)) if isinstance(audio_text, str) else ""
    audio_bound = (allow_audio_corroboration is True and isinstance(audio_sha256, str)
                   and bool(re.fullmatch(r"[a-f0-9]{64}", audio_sha256.lower())))
    audio_short_candidate = audio_bound and 3 <= cjk_count <= 6 and len(src) == cjk_count and audio_compact == dst
    if exceeds_default and not audio_short_candidate:
        return reject("too_many_substitutions_without_audio_corroboration" if allow_audio_corroboration else "too_many_substitutions")

    reads = proposal.get("caption_reads")
    if not isinstance(reads, list) or not 2 <= len(reads) <= 8:
        return reject("insufficient_caption_reads")
    normalized_reads: list[tuple[float, str]] = []
    seen_times: set[float] = set()
    for read in reads:
        if not isinstance(read, Mapping) or not isinstance(read.get("text"), str) or not read["text"].strip() or len(read["text"]) > 2000:
            return reject("invalid_caption_read")
        try:
            time = float(read["sample_time"])
        except (KeyError, ValueError, TypeError, OverflowError):
            return reject("invalid_caption_read_time")
        matches = [t for t in trusted_times if math.isfinite(time) and abs(t - time) <= 0.001]
        if not matches:
            return reject("unbound_caption_frame")
        trusted = min(matches, key=lambda t: abs(time - t))
        if trusted in seen_times:
            return reject("duplicate_caption_frame")
        seen_times.add(trusted)
        normalized_reads.append((trusted, "".join(char for _, char in _content_chars(read["text"]))))
    if max(seen_times) - min(seen_times) + 1e-9 < min_sample_span:
        return reject("insufficient_frame_span")

    if exceeds_default:
        # With no stable source neighbours left, a substring match is not
        # evidence. Require the entire caption in each supplied frame to agree
        # with the entire audio transcript and proposed short cue.
        if any(caption != dst for _, caption in normalized_reads):
            return reject("audio_caption_full_text_disagreement")
        audit.update(method="frame_bound_audio_caption_short_substitution_v1",
                     reason="full_audio_and_repeated_caption_agreement", audio_sha256=audio_sha256.lower(),
                     audio_transcript=audio_text, sample_times=sorted(seen_times),
                     source_cjk_characters=cjk_count,
                     evidence_limitation="Audio and image reads may come from the same model; not independent decoder agreement.")
        audit["substitutions"] = [{"position": pos, "from": src[pos], "to": dst[pos],
                                   "context": dst, "context_positions": [0, len(dst)],
                                   "supporting_times": sorted(seen_times)} for pos in changed]
        result_chars = list(original)
        for pos in changed:
            result_chars[src_map[pos][0]] = dst[pos]
        return {"accepted": True, "text": "".join(result_chars), "audit": audit}

    changes = set(changed)
    for pos in changed:
        # Two stable neighbours are required near the changed word, not copied
        # from a distant clause. Minimal supported context tolerates a caption
        # containing only this clause or extending beyond the ASR cue.
        contexts = []
        for left in range(max(0, pos - 3), pos + 1):
            for right in range(pos + 1, min(len(src), pos + 4) + 1):
                stable = [i for i in range(left, right) if i not in changes and _is_cjk(src[i])]
                if len(stable) < 2:
                    continue
                context = dst[left:right]
                wildcard = "".join("[\u3400-\u4dbf\u4e00-\u9fff]" if i in changes else re.escape(dst[i])
                                   for i in range(left, right))
                contexts.append((right - left, left, right, context, wildcard))
        accepted_context = None
        saw_conflict = False
        for _, left, right, context, wildcard in sorted(contexts):
            times = []
            conflict = False
            pattern = re.compile(f"(?=({wildcard}))")
            for time, caption in normalized_reads:
                hits = [match.group(1) for match in pattern.finditer(caption)]
                if any(hit != context for hit in hits):
                    conflict = True
                if context in hits:
                    times.append(time)
            if conflict:
                saw_conflict = True
                continue
            if len(times) >= 2 and max(times) - min(times) + 1e-9 >= min_sample_span:
                accepted_context = {"position": pos, "from": src[pos], "to": dst[pos],
                                    "context": context, "context_positions": [left, right],
                                    "supporting_times": sorted(times)}
                break
        if accepted_context is None:
            return reject("conflicting_caption_context" if saw_conflict else "insufficient_local_context_support")
        audit["substitutions"].append(accepted_context)

    result_chars = list(original)
    for pos in changed:
        result_chars[src_map[pos][0]] = dst[pos]
    audit["reason"] = "corroborated_small_substitution"
    audit["sample_times"] = sorted(seen_times)
    audit["source_cjk_characters"] = cjk_count
    return {"accepted": True, "text": "".join(result_chars), "audit": audit}
