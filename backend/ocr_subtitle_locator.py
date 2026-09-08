"""Select the Chinese subtitle band without following text printed on products."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from statistics import median
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple


DEFAULT_SUBTITLE_TOP = 0.75
DEFAULT_SUBTITLE_BOTTOM = 0.82


@dataclass(frozen=True)
class SubtitleBandSelection:
    top: float
    bottom: float
    mode: str
    support: int
    selected_by_segment: Mapping[int, Mapping[str, Any]]
    candidate_count: int
    selected_by_sample: Mapping[int, Sequence[Mapping[str, Any]]] = field(default_factory=dict)


@dataclass(frozen=True)
class _Candidate:
    block: Mapping[str, Any]
    sample_key: Any
    segment_id: Optional[int]
    normalized_text: str
    center_y: float
    geometry_score: float
    text_score: float
    strong_text_match: bool
    composite: bool = False

    @property
    def rank_score(self) -> float:
        return self.text_score * 2.5 + self.geometry_score + (
            1.0 if self.strong_text_match else 0.0
        ) + (0.25 if self.composite else 0.0)


def _value(block: Any, name: str, default: Any = None) -> Any:
    if isinstance(block, Mapping):
        return block.get(name, default)
    return getattr(block, name, default)


def _normalized_text(value: Any) -> str:
    return "".join(
        character.lower()
        for character in str(value or "")
        if character.isalnum() or "\u3400" <= character <= "\u9fff"
    )


def _chinese_text(value: Any) -> str:
    return "".join(
        character
        for character in str(value or "")
        if "\u3400" <= character <= "\u9fff"
    )


def _text_match(block_text: Any, speech_text: Any) -> Tuple[float, bool]:
    """Return a conservative substring score for an OCR line against ASR text."""

    block = _chinese_text(block_text)
    speech = _chinese_text(speech_text)
    if len(block) < 2 or len(speech) < 2:
        return 0.0, False

    matcher = SequenceMatcher(None, block, speech, autojunk=False)
    matching = [item for item in matcher.get_matching_blocks() if item.size]
    longest = max((item.size for item in matching), default=0)
    ordered = sum(item.size for item in matching)
    longest_coverage = longest / float(len(block))
    ordered_coverage = min(1.0, ordered / float(len(block)))
    ratio = matcher.ratio()
    contained = block in speech

    score = 0.65 * ratio + 0.35 * ordered_coverage
    strong = (len(block) == 2 and block == speech) or (len(block) >= 3 and (
        contained
        or (longest >= 3 and longest_coverage >= 0.45)
        or (ordered >= 4 and ordered_coverage >= 0.65 and ratio >= 0.22)
    )
    )
    return min(1.0, score), strong


def _geometry_score(
    block: Mapping[str, Any], frame_width: int, frame_height: int
) -> Optional[float]:
    try:
        left = float(_value(block, "x_pct", 0.0))
        right = float(_value(block, "max_x_pct", 0.0))
        top = float(_value(block, "y_pct", 0.0))
        bottom = float(_value(block, "max_y_pct", 0.0))
        probability = float(_value(block, "prob", 0.0) or 0.0)
    except (TypeError, ValueError):
        return None

    width = right - left
    height = bottom - top
    if not (0.0 <= left < right <= 1.0 and 0.03 <= top < bottom <= 0.96):
        return None
    if width < 0.025 or height < 0.007 or height > 0.14:
        return None

    pixel_aspect = (width * max(frame_width, 1)) / (
        height * max(frame_height, 1)
    )
    if pixel_aspect < 0.75:
        return None

    center_x = (left + right) * 0.5
    centered = max(0.0, 1.0 - abs(center_x - 0.5) / 0.5)
    horizontal = min(1.0, pixel_aspect / 6.0)
    useful_width = min(1.0, width / 0.45)
    confidence = max(0.0, min(1.0, probability))
    return 0.35 * centered + 0.30 * horizontal + 0.20 * useful_width + 0.15 * confidence


def _as_mapping(block: Any) -> Dict[str, Any]:
    return {
        "text": str(_value(block, "text", "")),
        "start": float(_value(block, "start", 0.0) or 0.0),
        "end": float(_value(block, "end", 0.0) or 0.0),
        "x_pct": float(_value(block, "x_pct", 0.0) or 0.0),
        "max_x_pct": float(_value(block, "max_x_pct", 0.0) or 0.0),
        "y_pct": float(_value(block, "y_pct", 0.0) or 0.0),
        "max_y_pct": float(_value(block, "max_y_pct", 0.0) or 0.0),
        "prob": float(_value(block, "prob", 0.0) or 0.0),
        "sample_segment_id": _value(block, "sample_segment_id"),
        "sample_time": float(_value(block, "sample_time", 0.0) or 0.0),
    }


def _combined_block(first: _Candidate, second: _Candidate) -> Dict[str, Any]:
    a, b = first.block, second.block
    return {
        "text": "{} {}".format(a["text"], b["text"]),
        "start": min(float(a["start"]), float(b["start"])),
        "end": max(float(a["end"]), float(b["end"])),
        "x_pct": min(float(a["x_pct"]), float(b["x_pct"])),
        "max_x_pct": max(float(a["max_x_pct"]), float(b["max_x_pct"])),
        "y_pct": min(float(a["y_pct"]), float(b["y_pct"])),
        "max_y_pct": max(float(a["max_y_pct"]), float(b["max_y_pct"])),
        "prob": max(float(a["prob"]), float(b["prob"])),
        "sample_segment_id": a.get("sample_segment_id"),
        "sample_time": a.get("sample_time", 0.0),
    }


def _cluster_candidates(
    candidates: Sequence[_Candidate], tolerance: float = 0.055
) -> List[List[_Candidate]]:
    clusters: List[List[_Candidate]] = []
    for candidate in sorted(candidates, key=lambda item: item.center_y):
        nearest = None
        nearest_distance = None
        for cluster in clusters:
            cluster_center = median(item.center_y for item in cluster)
            distance = abs(candidate.center_y - cluster_center)
            if distance <= tolerance and (
                nearest_distance is None or distance < nearest_distance
            ):
                nearest = cluster
                nearest_distance = distance
        if nearest is None:
            clusters.append([candidate])
        else:
            nearest.append(candidate)
    return clusters


def _best_per_sample(cluster: Sequence[_Candidate]) -> List[_Candidate]:
    best: Dict[Any, _Candidate] = {}
    for candidate in cluster:
        previous = best.get(candidate.sample_key)
        if previous is None or candidate.rank_score > previous.rank_score:
            best[candidate.sample_key] = candidate
    return list(best.values())


def _subtitle_zone_score(center_y: float) -> float:
    # In uncertain cases, prefer the normal title/subtitle zones over product text
    # in the middle of the image. This remains a weak signal; ASR matching wins.
    return min(1.0, abs(center_y - 0.5) / 0.25)


def _seed_cluster_rank(cluster: Sequence[_Candidate]) -> Tuple[float, ...]:
    chosen = _best_per_sample(cluster)
    center = median(item.center_y for item in chosen)
    return (
        float(len(chosen)),
        sum(item.text_score for item in chosen),
        sum(item.geometry_score for item in chosen) / len(chosen),
        _subtitle_zone_score(center),
    )


def _is_static_packaging_or_logo(cluster: Sequence[_Candidate]) -> bool:
    """Detect static packaging or logo text that repeats across samples without matching transcript."""
    chosen = _best_per_sample(cluster)
    if not chosen:
        return True

    # If any candidate has a strong text match to the ASR transcript, it's NOT packaging/logo
    if any(item.strong_text_match for item in chosen):
        return False

    best_text_score = max((item.text_score for item in chosen), default=0.0)
    if best_text_score >= 0.45:
        return False

    texts = Counter(item.normalized_text for item in chosen if item.normalized_text)
    unique_texts = len(texts)
    sample_count = len(chosen)

    # 1. Identical text repeating across >= 2 sampled segments with zero/low transcript match
    if sample_count >= 2 and unique_texts == 1 and best_text_score < 0.35:
        return True

    # 2. Static packaging text repeating across multiple segments with very low variability
    if sample_count >= 3 and (unique_texts / float(sample_count)) <= 0.5 and best_text_score < 0.35:
        return True

    return False


def _fallback_cluster_rank(cluster: Sequence[_Candidate]) -> Tuple[float, ...]:
    chosen = _best_per_sample(cluster)
    texts = Counter(item.normalized_text for item in chosen if item.normalized_text)
    unique_texts = len(texts)
    repetitions = max(0, len(chosen) - unique_texts)
    center = median(item.center_y for item in chosen)
    score = (
        len(chosen) * 1.5
        + unique_texts * 1.8
        + sum(item.text_score for item in chosen) * 1.5
        + sum(item.geometry_score for item in chosen) / max(len(chosen), 1)
        + _subtitle_zone_score(center) * 0.45
        - repetitions * 2.5
    )
    return (score, float(unique_texts), float(len(chosen)))


def select_chinese_subtitle_band(
    blocks: Iterable[Any],
    segment_texts: Mapping[int, str],
    frame_width: int,
    frame_height: int,
    default_top: float = DEFAULT_SUBTITLE_TOP,
    default_bottom: float = DEFAULT_SUBTITLE_BOTTOM,
) -> SubtitleBandSelection:
    """Find one spatial subtitle track and ignore unrelated Chinese scene text."""

    grouped: Dict[Any, List[_Candidate]] = {}
    candidates: List[_Candidate] = []
    speech_ids = list(segment_texts)
    context_texts = {
        sid: ''.join(segment_texts[key] for key in speech_ids[max(0, i-1):i+2])
        for i, sid in enumerate(speech_ids)
    }

    def speech_match(text, sid):
        score, strong = _text_match(text, segment_texts.get(sid, ''))
        if not strong:
            # A burnt-in caption often spans two ASR cues. Require a real
            # phrase in this cue before consulting its immediate neighbours.
            own = SequenceMatcher(None, _chinese_text(text),
                                  _chinese_text(segment_texts.get(sid, '')), autojunk=False)
            matches = own.get_matching_blocks()
            if max((m.size for m in matches), default=0) >= 3 and sum(m.size for m in matches) >= 4:
                context_score, context_strong = _text_match(text, context_texts.get(sid, ''))
                if context_strong and context_score >= .65:
                    return max(score, context_score*.9), True
        return score, strong

    for ordinal, source in enumerate(blocks):
        block = _as_mapping(source)
        if len(_chinese_text(block["text"])) < 2:
            continue
        geometry_score = _geometry_score(block, frame_width, frame_height)
        if geometry_score is None:
            continue
        raw_segment_id = block.get("sample_segment_id")
        segment_id = int(raw_segment_id) if raw_segment_id is not None else None
        sample_key = (
            ("segment", segment_id, round(float(block.get("sample_time", 0.0)), 4))
            if segment_id is not None
            else ("time", round(float(block.get("sample_time", ordinal)), 3))
        )
        text_score, strong = speech_match(block["text"], segment_id)
        candidate = _Candidate(
            block=block,
            sample_key=sample_key,
            segment_id=segment_id,
            normalized_text=_normalized_text(block["text"]),
            center_y=(block["y_pct"] + block["max_y_pct"]) * 0.5,
            geometry_score=geometry_score,
            text_score=text_score,
            strong_text_match=strong,
        )
        grouped.setdefault(sample_key, []).append(candidate)
        candidates.append(candidate)

    # Build two-line candidates only when the combined text strongly matches the
    # sampled speech. This covers real two-line subtitles without joining labels.
    composites: List[_Candidate] = []
    for sample_candidates in grouped.values():
        ordered = sorted(sample_candidates, key=lambda item: item.block["y_pct"])
        for index, first in enumerate(ordered):
            for second in ordered[index + 1 :]:
                vertical_gap = second.block["y_pct"] - first.block["max_y_pct"]
                same_line = abs(first.center_y - second.center_y) <= 0.012
                horizontal_gap = max(0.0,
                    second.block["x_pct"] - first.block["max_x_pct"],
                    first.block["x_pct"] - second.block["max_x_pct"])
                if not same_line and (vertical_gap < -0.01 or vertical_gap > 0.035):
                    continue
                if same_line and horizontal_gap > 0.04:
                    continue
                ordered_pair = sorted((first, second), key=lambda c: c.block["x_pct"]) if same_line else (first, second)
                combined = _combined_block(*ordered_pair)
                if combined["max_y_pct"] - combined["y_pct"] > 0.16:
                    continue
                horizontal_overlap = not (
                    first.block["max_x_pct"] < second.block["x_pct"]
                    or second.block["max_x_pct"] < first.block["x_pct"]
                )
                first_center = (first.block["x_pct"] + first.block["max_x_pct"]) * 0.5
                second_center = (second.block["x_pct"] + second.block["max_x_pct"]) * 0.5
                if not same_line and not horizontal_overlap and abs(first_center - second_center) > 0.18:
                    continue
                segment_id = first.segment_id
                text_score, strong = _text_match(
                    combined["text"], segment_texts.get(segment_id, "")
                )
                speech = _chinese_text(segment_texts.get(segment_id, ""))
                combined_ratio = SequenceMatcher(None, _chinese_text(combined["text"]), speech, autojunk=False).ratio()
                single_ratio = max(SequenceMatcher(None, _chinese_text(c.block["text"]), speech,
                                    autojunk=False).ratio() for c in (first, second))
                if combined_ratio <= single_ratio or not strong or text_score + 0.04 < max(
                    first.text_score, second.text_score
                ):
                    continue
                geometry_score = _geometry_score(combined, frame_width, frame_height)
                if geometry_score is None:
                    continue
                composites.append(
                    _Candidate(
                        block=combined,
                        sample_key=first.sample_key,
                        segment_id=segment_id,
                        normalized_text=_normalized_text(combined["text"]),
                        center_y=(combined["y_pct"] + combined["max_y_pct"]) * 0.5,
                        geometry_score=geometry_score,
                        text_score=text_score,
                        strong_text_match=True,
                        composite=True,
                    )
                )
    candidates.extend(composites)

    if not candidates:
        return SubtitleBandSelection(
            default_top, default_bottom, "default", 0, {}, 0
        )

    def reliable(item):
        related = [
            other for other in candidates if not other.composite
            and abs(other.center_y - item.center_y) <= 0.035
            and abs((other.block["x_pct"] + other.block["max_x_pct"]) -
                    (item.block["x_pct"] + item.block["max_x_pct"])) <= 0.10
            and SequenceMatcher(None, other.normalized_text, item.normalized_text,
                                autojunk=False).ratio() >= 0.72
        ]
        seen_segments = {other.segment_id for other in related}
        matching_segments = {other.segment_id for other in related if other.strong_text_match}
        return not (len(seen_segments) >= 3 and
                    len(matching_segments) / len(seen_segments) < 0.6)

    strong_candidates = [item for item in candidates if item.strong_text_match and reliable(item)]
    chosen_cluster: Optional[List[_Candidate]] = None
    mode = "asr_match"
    if strong_candidates:
        seed_clusters = _cluster_candidates(strong_candidates)
        valid_seeds = [c for c in seed_clusters if not _is_static_packaging_or_logo(c)]
        if valid_seeds:
            best_seed = max(valid_seeds, key=_seed_cluster_rank)
            seed_support = len(_best_per_sample(best_seed))
            sample_count = len({item.sample_key for item in candidates})
            best_text_score = max(item.text_score for item in best_seed)
            if seed_support >= 2 or sample_count <= 2 or best_text_score >= 0.85:
                chosen_cluster = best_seed

    # Spatial variation alone must not seed a subtitle band.

    if not chosen_cluster:
        return SubtitleBandSelection(
            default_top,
            default_bottom,
            "default",
            0,
            {},
            len(candidates),
        )

    chosen = _best_per_sample(chosen_cluster)
    center = median(item.center_y for item in chosen)
    # The selected cluster has already excluded unrelated scene text. Use its
    # outer bounds so glyph outlines and small vertical subtitle motion are not
    # left visible in segments away from the median frame.
    top = min(float(item.block["y_pct"]) for item in chosen)
    bottom = max(float(item.block["max_y_pct"]) for item in chosen)
    height = bottom - top
    if height < 0.025:
        top, bottom = center - 0.0125, center + 0.0125
    elif height > 0.14:
        top, bottom = center - 0.07, center + 0.07
    top = max(0.03, top)
    bottom = min(0.96, bottom)

    selected_by_segment: Dict[int, Mapping[str, Any]] = {}
    selected_by_sample: Dict[int, List[Mapping[str, Any]]] = {}
    # Outlined fonts can produce near-zero OCR confidence and corrupt words.
    # Recover only an observed centered line bracketed by ASR-validated lines
    # at the same position. Never invent a box for an empty frame.
    def bracketed_line(item):
        b = item.block
        if (item.composite or not reliable(item)
                or len(_chinese_text(b["text"])) < 4
                or b["max_x_pct"] - b["x_pct"] < 0.30
                or abs((b["x_pct"] + b["max_x_pct"]) / 2 - 0.5) > 0.10):
            return False
        anchors = [other for other in strong_candidates
                   if other.segment_id != item.segment_id
                   and abs(other.center_y - item.center_y) <= 0.018
                   and abs((other.block["max_y_pct"] - other.block["y_pct"])
                           - (b["max_y_pct"] - b["y_pct"])) <= 0.022]
        t = b["sample_time"]
        return (any(0 < t - a.block["sample_time"] <= 6 for a in anchors)
                and any(0 < a.block["sample_time"] - t <= 6 for a in anchors))

    recovered = [item for item in candidates
                 if not item.strong_text_match and bracketed_line(item)]
    for segment_id in segment_texts:
        # Use only validated matches, including genuine vertical motion.
        local = [item for item in strong_candidates + recovered if item.segment_id == segment_id]
        selected = sorted(_best_per_sample(local), key=lambda item: item.block["sample_time"])
        if not selected:
            continue
        selected_by_sample[int(segment_id)] = [dict(item.block) for item in selected]
        selected_by_segment[int(segment_id)] = dict(max(selected, key=lambda item: item.rank_score).block)

    return SubtitleBandSelection(
        top=top,
        bottom=bottom,
        mode=mode,
        support=len(chosen),
        selected_by_segment=selected_by_segment,
        candidate_count=len(candidates),
        selected_by_sample=selected_by_sample,
    )
