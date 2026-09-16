import math
import re
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime, timezone

from backend.schemas.contracts import VideoResult
from backend.services.translator import (
    ACTION_MAP,
    MANDATORY_MAP,
    SUBJECT_MAP,
    extract_mandatory_attributes,
    is_valid_action_match,
    is_valid_subject_match,
    normalize_vietnamese,
)

GENERIC_MODIFIERS = {
    "cute",
    "de thuong",
    "dang yeu",
    "xinh",
    "dep",
    "vlog",
    "review",
    "danh gia",
    "share",
    "daily",
    "ootd",
    "haul",
    "可爱",
    "萌物",
    "少女心",
    "高颜值",
    "好看",
    "日常",
    "测评",
    "好物",
    "分享",
    "灵感",
    "沉浸式",
}


@dataclass
class RankBreakdown:
    relevance: float
    quality: float
    final_score: float
    matched_query: str = ""
    matched_tokens: list[str] = field(default_factory=list)
    missing_core_tokens: list[str] = field(default_factory=list)


def tokens(value: str) -> set[str]:
    normalized = unicodedata.normalize("NFKD", value.lower()).replace("đ", "d")
    normalized = "".join(c for c in normalized if not unicodedata.combining(c))
    words = set(re.findall(r"[^\W_]+", normalized))
    for sequence in re.findall(r"[\u4e00-\u9fff]+", normalized):
        for char in sequence:
            words.add(char)
        words.update(sequence[i : i + 2] for i in range(len(sequence) - 1))
    return words


def phrase_matches(text: str, phrase: str) -> bool:
    """Match Chinese phrases or whole Latin words, with Vietnamese accent folding."""
    text = normalize_vietnamese(text)
    phrase = normalize_vietnamese(phrase)
    if re.search(r"[\u4e00-\u9fff]", phrase):
        return phrase in text
    return bool(re.search(r"(?<!\w)" + re.escape(phrase) + r"(?!\w)", text))


def intent_constraints(query: str) -> list[tuple[str, list[str], float]]:
    """Keep expanded queries subordinate to the original, known search intent.

    This is dictionary evidence, not semantic understanding. Unknown subjects
    retain lexical scoring. No translation service or AI call is made here.
    """
    groups: list[tuple[str, list[str], float]] = []
    norm = normalize_vietnamese(query)
    for name in sorted(SUBJECT_MAP, key=len, reverse=True):
        aliases = [alias for tier in SUBJECT_MAP[name].values() for alias in tier]
        if phrase_matches(query, name) and is_valid_subject_match(query, norm, name):
            groups.append((name, [name, *aliases], 20.0))
            break
    else:
        # Use primary terms for recognition: broad expansion terms such as
        # "popular shades" must not identify a specific product by themselves.
        matches = [
            (alias, name) for name, tiers in SUBJECT_MAP.items()
            for alias in tiers["primary"] if phrase_matches(query, alias)
        ]
        if matches:
            _, name = max(matches, key=lambda item: len(item[0]))
            aliases = [alias for tier in SUBJECT_MAP[name].values() for alias in tier]
            groups.append((name, [name, *aliases], 20.0))
    for name in sorted(ACTION_MAP, key=len, reverse=True):
        aliases = ACTION_MAP[name]
        if (
            phrase_matches(query, name)
            or any(phrase_matches(query, a) for a in aliases)
        ) and is_valid_action_match(query, norm, name):
            # Include equivalent Vietnamese action names (unbox / dap hop).
            equivalents = [key for key, values in ACTION_MAP.items() if set(values) & set(aliases)]
            groups.append((name, [*equivalents, *aliases], 45.0))
            break
    names, attributes = extract_mandatory_attributes(norm)
    for name, aliases in zip(names, attributes, strict=True):
        groups.append((name, [name, *aliases], 40.0))
    for name, aliases in MANDATORY_MAP.items():
        if name not in names and any(phrase_matches(query, a) for a in aliases):
            groups.append((name, [name, *aliases], 40.0))
    return groups


def rank_detailed(
    result: VideoResult,
    query: str,
    alternative_queries: list[str] | None = None,
) -> RankBreakdown:
    candidates = [query]
    if alternative_queries:
        for alt in alternative_queries:
            if alt and alt not in candidates:
                candidates.append(alt)

    title = tokens(result.title or "")
    caption = tokens(result.caption or "")
    tags = tokens(" ".join(result.hashtags))
    target_tokens = title | caption | tags

    best_relevance = 0.0
    best_cand = candidates[0]
    best_matched_tokens: list[str] = []
    best_missing_core: list[str] = []

    for cand in candidates:
        wanted = tokens(cand)
        if not wanted:
            continue
        core_wanted = wanted - GENERIC_MODIFIERS
        core_matched = core_wanted & target_tokens
        modifier_wanted = wanted & GENERIC_MODIFIERS
        matched = wanted & target_tokens

        core_bigrams = {x for x in core_wanted if len(x) >= 2}
        if core_bigrams:
            core_bigrams_matched = core_bigrams & target_tokens
            has_core = bool(core_bigrams_matched)
        else:
            has_core = bool(core_matched)

        if core_wanted:
            if not has_core:
                # Phạt nặng video không chứa bất kỳ từ khóa/cụm từ chủ thể nào
                rel = min(20.0, 25.0 * (len(matched) / max(len(wanted), 1)))
            else:
                if core_bigrams:
                    core_coverage = len(core_bigrams_matched) / len(core_bigrams)
                    title_core = len(core_bigrams_matched & title) / len(core_bigrams)
                else:
                    core_coverage = len(core_matched) / len(core_wanted)
                    title_core = len(core_matched & title) / len(core_wanted)
                mod_coverage = (
                    len(modifier_wanted & target_tokens) / len(modifier_wanted)
                    if modifier_wanted
                    else 1.0
                )
                rel = min(100.0, 60.0 * core_coverage + 25.0 * title_core + 15.0 * mod_coverage)
        else:
            coverage = len(matched) / max(len(wanted), 1)
            title_match = len(wanted & title) / max(len(wanted), 1)
            rel = min(100.0, 80 * coverage + 20 * title_match)

        if rel > best_relevance:
            best_relevance = rel
            best_cand = cand
            best_matched_tokens = sorted(matched)
            best_missing_core = sorted(core_wanted - core_matched)

    relevance = best_relevance
    if len(candidates) > 1 and best_relevance >= 40.0:
        consensus_matches = 0
        for alt in candidates:
            w = tokens(alt) - GENERIC_MODIFIERS
            if w and (w & target_tokens):
                consensus_matches += 1
        if consensus_matches >= 2:
            relevance = min(100.0, relevance + 5.0)

    # Do not let a broad alternative erase the original subject or attributes.
    evidence = [result.title or "", result.caption or "", *result.hashtags]
    for name, aliases, ceiling in intent_constraints(query):
        if not any(phrase_matches(part, alias) for part in evidence for alias in aliases):
            relevance = min(relevance, ceiling)
            best_missing_core.append(name)
    metrics = [
        (result.like_count, 100000),
        (result.comment_count, 5000),
        (result.favorite_count, 20000),
        (result.share_count, 10000),
    ]
    known = [
        min(1, math.log1p(value) / math.log1p(cap)) for value, cap in metrics if value is not None
    ]
    engagement = sum(known) / len(known) if known else 0
    freshness = 0.0
    if result.published_at:
        published = (
            result.published_at.replace(tzinfo=timezone.utc)
            if result.published_at.tzinfo is None
            else result.published_at
        )
        days = max(0, (datetime.now(timezone.utc) - published).days)
        freshness = math.exp(-days / 60)
    quality = 100 * (0.85 * engagement + 0.15 * freshness)
    final_score = relevance * (0.8 + 0.2 * quality / 100)

    return RankBreakdown(
        relevance=round(relevance, 2),
        quality=round(quality, 2),
        final_score=round(final_score, 2),
        matched_query=best_cand,
        matched_tokens=best_matched_tokens,
        missing_core_tokens=best_missing_core,
    )


def rank(
    result: VideoResult,
    query: str,
    alternative_queries: list[str] | None = None,
) -> tuple[float, float, float]:
    detail = rank_detailed(result, query, alternative_queries=alternative_queries)
    return detail.relevance, detail.quality, detail.final_score
