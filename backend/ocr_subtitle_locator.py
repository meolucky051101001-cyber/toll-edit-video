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

    norm_b = str(block_text or "").lower()
    norm_s = str(speech_text or "").lower()
    for src, dst in (("10000", "一万"), ("1000", "一千"), ("100", "一百"), ("10", "十"), ("5k", "五千"), ("1k", "一千"), ("两", "二"), ("0", "零"), ("1", "一"), ("2", "二"), ("3", "三"), ("4", "四"), ("5", "五"), ("6", "六"), ("7", "七"), ("8", "八"), ("9", "九")):
        norm_b = norm_b.replace(src, dst)
        norm_s = norm_s.replace(src, dst)
    nb_cjk = _chinese_text(norm_b)
    ns_cjk = _chinese_text(norm_s)
    if (nb_cjk != block or ns_cjk != speech) and len(nb_cjk) >= 2 and len(ns_cjk) >= 2:
        nm = SequenceMatcher(None, nb_cjk, ns_cjk, autojunk=False)
        n_matching = [item for item in nm.get_matching_blocks() if item.size]
        n_longest = max((item.size for item in n_matching), default=0)
        n_ordered = sum(item.size for item in n_matching)
        longest = max(longest, n_longest)
        ordered = max(ordered, n_ordered)
        longest_coverage = max(longest_coverage, n_longest / float(len(nb_cjk)))
        ordered_coverage = max(ordered_coverage, min(1.0, n_ordered / float(len(nb_cjk))))
        ratio = max(ratio, nm.ratio())
        contained = contained or (nb_cjk in ns_cjk)
        score = max(score, 0.65 * ratio + 0.35 * ordered_coverage)

    import re
    b_ascii = set(re.findall(r"[a-z]{2,}|\d{2,}", str(block_text or "").lower()))
    s_ascii = set(re.findall(r"[a-z]{2,}|\d{2,}", str(speech_text or "").lower()))
    shared_ascii = bool(b_ascii & s_ascii) and ordered >= 2
    short_homophone = len(block) == 3 and (longest >= 2 or (ordered >= 2 and block[0] in speech and block[-1] in speech))
    if shared_ascii or short_homophone:
        score = max(score, 0.56)

    strong = (len(block) == 2 and (block == speech or block in speech)) or short_homophone or shared_ascii or (len(block) >= 3 and (
        contained
        or (longest >= 3 and longest_coverage >= 0.45)
        or (ordered >= 4 and ordered_coverage >= 0.55 and ratio >= 0.20)
        or (ordered >= 4 and ratio >= 0.50)
    )
    )
    return min(1.0, score), strong


def _geometry_score(
    block: Mapping[str, Any], frame_width: int, frame_height: int,
    max_height: float = 0.14,
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
    # Captions may touch either edge; do not silently reject the bottom 4%.
    if not (0.0 <= left < right <= 1.0 and 0.0 <= top < bottom <= 1.0):
        return None
    # Burned-in captions can legitimately run almost edge-to-edge (the source
    # may even crop the first/last glyph).  Rejecting boxes wider than 90%
    # drops their geometry and makes ASS fall back to text-only rendering with
    # no white cover.  Wide scene/package text is still protected by the
    # transcript-match and static-text checks below, so width alone must not
    # disqualify an otherwise valid ASR-matched subtitle.
    if width < 0.025 or height < 0.007 or height > max_height:
        return None

    cjk_count = len(_chinese_text(_value(block, "text", "")))
    min_aspect = 0.55 if cjk_count >= 4 else 0.75
    pixel_aspect = (width * max(frame_width, 1)) / (
        height * max(frame_height, 1)
    )
    if pixel_aspect < min_aspect:
        return None

    center_x = (left + right) * 0.5
    if cjk_count <= 2 and height > 0.085 and abs(center_x - 0.5) > 0.18:
        return None
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
        "sample_in_cue": bool(_value(block, "sample_in_cue", True)),
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

    center = median(item.center_y for item in chosen)
    # Mid-screen zone (0.25 - 0.68) is product packaging/scene text zone unless strongly matched to ASR
    if 0.25 <= center <= 0.68 and best_text_score < 0.40:
        return True

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


def _distinct_caption_count(samples: Sequence[_Candidate]) -> int:
    """Count visually distinct captions, tolerating OCR jitter on the same line."""
    distinct: List[str] = []
    for sample in samples:
        text = sample.normalized_text
        if not text:
            continue
        if any(
            SequenceMatcher(None, existing, text, autojunk=False).ratio() >= 0.72
            for existing in distinct
        ):
            continue
        distinct.append(text)
    return len(distinct)


def _has_watermark_tokens(text: Any) -> bool:
    raw = str(text or "")
    if raw.lstrip().startswith(("@", "＠")):
        return True
    return any(token in raw for token in ("抖音", "小红书", "快手", "关注", "点赞", "收藏"))


def _is_static_visual_cluster(cluster: Sequence[_Candidate], duration: float = 0.0) -> bool:
    """Reject banners, shop names and watermarks that never change content."""
    samples = _best_per_sample(cluster)
    if not samples:
        return True
    times = [float(item.block.get("sample_time", 0.0) or 0.0) for item in samples]
    span = max(times) - min(times) if times else 0.0
    distinct = _distinct_caption_count(samples)
    token_hits = sum(1 for item in samples if _has_watermark_tokens(item.block.get("text", "")))
    if token_hits >= max(2, int(len(samples) * 0.5)):
        return True
    if distinct <= 2:
        if span >= 8.5:
            return True
        if duration >= 15.0 and span >= duration * 0.32:
            return True
        if duration > 0 and len(samples) >= 8 and span >= duration * 0.55:
            return True
    return False


def _choose_visual_band(
    candidates: Sequence[_Candidate],
    duration: float = 0.0,
    visual_only_mode: bool = False,
    reliable: Optional[Any] = None,
) -> Tuple[Optional[List[_Candidate]], bool]:
    """Pick the dynamic caption row; visual-only mode ignores static packaging."""
    if not candidates:
        return None, False
    is_reliable = reliable or (lambda item: True)
    all_clusters = _cluster_candidates(
        [item for item in candidates if not item.composite and is_reliable(item)]
    )
    # Visual-only mode must not apply the ASR-oriented "low text variability"
    # packaging test: a real subtitle track repeats each caption across several
    # dense samples, so low variability is expected there.
    valid_clusters = [
        cluster for cluster in all_clusters
        if not (_is_static_visual_cluster(cluster, duration) if visual_only_mode
                else _is_static_packaging_or_logo(cluster))
    ]
    ranked: List[Tuple[Tuple[float, ...], List[_Candidate]]] = []
    for cluster in valid_clusters:
        samples = _best_per_sample(cluster)
        if len(samples) < 2:
            continue
        med_cx = median((c.block["x_pct"] + c.block["max_x_pct"]) * 0.5 for c in samples)
        if abs(med_cx - 0.5) > (0.25 if visual_only_mode else 0.12):
            continue
        med_h = median(c.block["max_y_pct"] - c.block["y_pct"] for c in samples)
        if not (0.02 <= med_h <= 0.10):
            continue
        if visual_only_mode:
            if _distinct_caption_count(samples) < 2:
                continue
            med_cy = median(item.center_y for item in samples)
            if not (0.30 <= med_cy <= 0.95):
                continue
            if _is_static_visual_cluster(cluster, duration):
                continue
            zone = 1.0 if 0.42 <= med_cy <= 0.92 else 0.0
            centered = max(0.0, 1.0 - abs(med_cx - 0.5) / 0.25)
            rank: Tuple[float, ...] = (
                zone,
                float(_distinct_caption_count(samples)),
                float(len(samples)),
                centered,
            )
        else:
            unique_texts = {c.normalized_text for c in samples}
            if len(unique_texts) < 2:
                continue
            rank = (float(len(samples)),)
        ranked.append((rank, cluster))
    if not ranked:
        return None, False
    ranked.sort(key=lambda item: item[0], reverse=True)
    return ranked[0][1], True


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
    allow_visual_only: bool = False,
    duration: Optional[float] = None,
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
            # A burnt-in caption often spans two ASR cues; only borrow neighbour
            # speech when this cue already shares a meaningful Chinese phrase.
            own = SequenceMatcher(None, _chinese_text(text),
                                  _chinese_text(segment_texts.get(sid, '')), autojunk=False)
            matches = own.get_matching_blocks()
            if max((m.size for m in matches), default=0) >= 3 and sum(m.size for m in matches) >= 4:
                context_score, context_strong = _text_match(text, context_texts.get(sid, ''))
                if context_strong and context_score >= .65:
                    return max(score, context_score*.9), True
        return score, strong

    for ordinal, source in enumerate(blocks):
        # Respect explicit classification before mapping drops optional fields.
        if (_value(source, "is_subtitle") is False
                or _value(source, "is_packaging") is True
                or _value(source, "is_static") is True
                or _value(source, "in_subtitle_band") is False
                or str(_value(source, "type", "")).lower() in
                ("packaging", "background", "logo", "watermark")):
            continue
        block = _as_mapping(source)
        if block["text"].lstrip().startswith(("@", "＠")) or len(_chinese_text(block["text"])) < 2:
            continue
        geometry_score = _geometry_score(block, frame_width, frame_height, max_height=.24)
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
        if block['max_y_pct'] - block['y_pct'] > .14 and not strong:
            continue
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
                if not same_line and (vertical_gap < -0.035 or vertical_gap > 0.040):
                    continue
                if same_line and horizontal_gap > 0.04:
                    continue
                ordered_pair = sorted((first, second), key=lambda c: c.block["x_pct"]) if same_line else (first, second)
                combined = _combined_block(*ordered_pair)
                if combined["max_y_pct"] - combined["y_pct"] > 0.30:
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
                geometry_score = _geometry_score(combined, frame_width, frame_height, max_height=0.30)
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

    reliability_cache = {}
    def reliable(item):
        cached = reliability_cache.get(id(item))
        if cached is not None:
            return cached
        related = [
            other for other in candidates if not other.composite
            and other.block.get('sample_in_cue', True)
            and abs(other.center_y - item.center_y) <= 0.035
            and abs((other.block["x_pct"] + other.block["max_x_pct"]) -
                    (item.block["x_pct"] + item.block["max_x_pct"])) <= 0.10
            and SequenceMatcher(None, other.normalized_text, item.normalized_text,
                                autojunk=False).ratio() >= 0.72
        ]
        seen_segments = {other.segment_id for other in related}
        matching_segments = {other.segment_id for other in related if other.strong_text_match}
        result = not ((len(seen_segments) >= 2 and not matching_segments) or
                      (len(seen_segments) >= 3 and
                       len(matching_segments) / len(seen_segments) < 0.6))
        if not result and item.strong_text_match:
            # Imported observations may lack cue bounds. A caption bracketed
            # by two different matched rows is a transition, not a static label.
            times = [other.block['sample_time'] for other in related]
            before = [other for other in candidates if not other.composite
                      and other.strong_text_match and other.segment_id not in matching_segments
                      and abs(other.center_y-item.center_y) <= .035
                      and 0 < min(times)-other.block['sample_time'] <= 1.5
                      and other.normalized_text != item.normalized_text]
            after = [other for other in candidates if not other.composite
                     and other.strong_text_match and other.segment_id not in matching_segments
                     and abs(other.center_y-item.center_y) <= .035
                     and 0 < other.block['sample_time']-max(times) <= 1.5
                     and other.normalized_text != item.normalized_text]
            result = any(a.segment_id != b.segment_id and
                         a.normalized_text != b.normalized_text for a in before for b in after)
        reliability_cache[id(item)] = result
        return result

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

    # Spatial variation alone must not seed a subtitle band unless real speech is present
    # (or the opt-in visual-only mode is active) and dynamic, centered Chinese
    # subtitles are visually detected across segments.
    visual_only_mode = False
    if not chosen_cluster:
        has_real_speech = any(
            len(_chinese_text(s)) >= 2 and s.strip().lower() != "speech unavailable"
            for s in segment_texts.values()
        )
        if (has_real_speech or allow_visual_only) and candidates:
            effective_duration = duration
            if effective_duration is None:
                effective_duration = max(
                    (
                        float(c.block.get("sample_time", 0.0) or 0.0)
                        for c in candidates
                    ),
                    default=0.0,
                ) + 0.5
            chosen_cluster, visual_selected = _choose_visual_band(
                candidates,
                effective_duration,
                visual_only_mode=not has_real_speech,
                reliable=reliable,
            )
            if chosen_cluster and visual_selected:
                mode = "visual_match"
                visual_only_mode = not has_real_speech

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
    top = max(0.0, top)
    bottom = min(1.0, bottom)

    selected_by_segment: Dict[int, Mapping[str, Any]] = {}
    selected_by_sample: Dict[int, List[Mapping[str, Any]]] = {}
    strong_by_segment = {}
    if mode == "visual_match" and chosen_cluster:
        for item in chosen_cluster:
            if item.segment_id is not None:
                strong_by_segment.setdefault(item.segment_id, []).append(item)
    else:
        for item in strong_candidates:
            strong_by_segment.setdefault(item.segment_id, []).append(item)
    if mode == "asr_match":
        # A corrupt recognition may still locate a row precisely. Require both
        # neighbouring cues to confirm this track before reusing observed bounds.
        for position, segment_id in enumerate(speech_ids):
            if strong_by_segment.get(segment_id):
                continue
            previous = strong_by_segment.get(speech_ids[position - 1], []) if position > 0 else []
            following = strong_by_segment.get(speech_ids[position + 1], []) if position + 1 < len(speech_ids) else []
            if not previous and not following:
                continue
            if (not previous or not following) and len(strong_by_segment) < 2:
                continue
            anchors = previous + following
            anchor_center = median(item.center_y for item in anchors)
            anchor_height = median(item.block["max_y_pct"] - item.block["y_pct"] for item in anchors)
            observed = [item for item in candidates if not item.composite
                        and item.segment_id == segment_id and reliable(item)
                        and len(_chinese_text(item.block["text"])) >= 4
                        and abs(item.center_y - anchor_center) <= 0.025
                        and abs(anchor_center - center) <= 0.035
                        and item.block["max_x_pct"] - item.block["x_pct"] >= 0.25
                        and 0.7 * anchor_height <= item.block["max_y_pct"] - item.block["y_pct"] <= 1.5 * anchor_height]
            if observed:
                strong_by_segment[segment_id] = observed
    for segment_id in segment_texts:
        # Use only validated matches, including genuine vertical motion.
        local = strong_by_segment.get(segment_id, [])
        selected = sorted(_best_per_sample(local), key=lambda item: item.block["sample_time"])
        if not selected:
            continue
        # An ASR cue may describe only part of one long on-screen sentence.
        # Match text to establish the anchor, then cover the complete co-linear
        # row at THAT sample (never union different timestamps or nearby rows).
        expanded = []
        for anchor in selected:
            merged = dict(anchor.block)
            members = [anchor]
            pool = [item for item in grouped.get(anchor.sample_key, [])
                    if reliable(item)]
            changed = True
            while changed:
                changed = False
                for item in pool:
                    if item in members:
                        continue
                    a, b = merged, item.block
                    height_a = float(anchor.block['max_y_pct']) - float(anchor.block['y_pct'])
                    height_b = float(b['max_y_pct']) - float(b['y_pct'])
                    same_baseline = abs(anchor.center_y - item.center_y) <= min(.012, max(height_a, height_b) * .3)
                    gap = max(0.0, float(b['x_pct']) - float(a['max_x_pct']),
                              float(a['x_pct']) - float(b['max_x_pct']))
                    overlap = min(float(a['max_x_pct']), float(b['max_x_pct'])) - max(float(a['x_pct']), float(b['x_pct']))
                    if not same_baseline or gap > .045 or max(height_a, height_b) > min(height_a, height_b) * 1.8:
                        continue
                    # OCR glyph margins intentionally overlap at word seams.
                    # Reject deep containment, not a few padded pixels.
                    if overlap > .06 and not item.strong_text_match:
                        continue
                    members.append(item)
                    merged.update(x_pct=min(a['x_pct'], b['x_pct']),
                                  max_x_pct=max(a['max_x_pct'], b['max_x_pct']),
                                  y_pct=min(a['y_pct'], b['y_pct']),
                                  max_y_pct=max(a['max_y_pct'], b['max_y_pct']))
                    changed = True
            merged['text'] = ' '.join(item.block['text'] for item in sorted(members, key=lambda c:c.block['x_pct']))
            expanded.append(merged)
        selected_by_sample[int(segment_id)] = expanded
        best = max(selected, key=lambda item: item.rank_score)
        selected_by_segment[int(segment_id)] = expanded[selected.index(best)]

    if visual_only_mode and chosen_cluster:
        # No transcript exists to anchor rows to cues. Expose the selected
        # surface captions by sample so OCR can build tracking blocks for the
        # single synthetic visual cue.
        visual_samples: Dict[int, List[Mapping[str, Any]]] = {}
        for item in chosen_cluster:
            sample_id = int(item.segment_id) if item.segment_id is not None else 0
            visual_samples.setdefault(sample_id, []).append(dict(item.block))
        for sample_blocks in visual_samples.values():
            sample_blocks.sort(key=lambda block: float(block.get("sample_time", 0.0) or 0.0))
        selected_by_sample = visual_samples

    return SubtitleBandSelection(
        top=top,
        bottom=bottom,
        mode=mode,
        support=len(chosen),
        selected_by_segment=selected_by_segment,
        candidate_count=len(candidates),
        selected_by_sample=selected_by_sample,
    )
