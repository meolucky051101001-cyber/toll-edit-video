import math
import re
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime, timezone

from backend.schemas.contracts import VideoResult
from backend.services.translator import (
    ACTION_MAP,
    HASHTAG_SEGMENTATION_MAP,
    MANDATORY_MAP,
    SUBJECT_MAP,
    compound_intent,
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
    virality_score: float = 0.0
    velocity_score: float = 0.0
    matched_query: str = ""
    matched_tokens: list[str] = field(default_factory=list)
    missing_core_tokens: list[str] = field(default_factory=list)
    missing_mandatory_intents: list[str] = field(default_factory=list)
    provenance: dict[str, list[str]] = field(default_factory=dict)
    matched_negative_tokens: list[str] = field(default_factory=list)


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
    if bool(re.search(r"(?<!\w)" + re.escape(phrase) + r"(?!\w)", text)):
        return True
    clean_text = text.replace("_", "").replace("-", "")
    clean_phrase = phrase.replace(" ", "")
    if clean_phrase and len(clean_phrase) >= 3 and clean_phrase in clean_text:
        return True
    return False


def intent_constraints(query: str) -> list[tuple[str, list[str], float]]:
    """Keep expanded queries subordinate to the original, known search intent.

    This is dictionary evidence, not semantic understanding. Unknown subjects
    retain lexical scoring. No translation service or AI call is made here.
    """
    groups: list[tuple[str, list[str], float]] = []
    norm = normalize_vietnamese(query)
    compound = compound_intent(query)
    if compound:
        for aliases in compound[0]:
            groups.append((aliases[0], aliases, 20.0))
    else:
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


def normalize_hashtag_str(tag: str) -> str:
    cleaned = normalize_vietnamese(tag).lstrip("#").strip().lower()
    return cleaned.replace("_", "").replace("-", "").replace(" ", "")


HASHTAG_ALIAS_MAP: dict[str, set[str]] = {
    "decorbanhoc": {"decorbanhoc", "trangtribanhoc", "gocbanhoc", "书桌布置", "桌面改造", "桌搭"},
    "decorphongngu": {"decorphongngu", "trangtriphongngu", "gocphongngu", "卧室布置", "房间布置"},
    "decorbanlamviec": {"decorbanlamviec", "trangtribanlamviec", "goclamviec", "办公桌布置", "工位改造"},
    "banphimco": {"banphimco", "banphim", "机械键盘", "客制化键盘"},
    "meotrimunan": {"meotrimunan", "trimunan", "去闭口", "祛闭口"},
    "trimun": {"trimun", "tri_mun", "祛痘", "战痘"},
    "kemchongnang": {"kemchongnang", "防晒", "防晒霜"},
    "suaruamat": {"suaruamat", "洁面", "洗面奶"},
    "duongda": {"duongda", "护肤", "护肤流程"},
    "phoido": {"phoido", "穿搭", "日常穿搭", "ootd"},
    "ootd": {"ootd", "dailyootd", "今日穿搭", "穿搭"},
}

CANONICAL_HASHTAG_MAP: dict[str, str] = {}
for canonical_tag, tag_aliases in HASHTAG_ALIAS_MAP.items():
    for a in tag_aliases:
        CANONICAL_HASHTAG_MAP[normalize_hashtag_str(a)] = canonical_tag


def rank_detailed(

    result: VideoResult,
    query: str,
    alternative_queries: list[str] | None = None,
    negative_keywords: list[str] | None = None,
    exact_hashtag: bool = False,
    target_hashtag: str | None = None,
) -> RankBreakdown:
    candidates = [query]
    norm_q = normalize_hashtag_str(query)
    if norm_q in HASHTAG_SEGMENTATION_MAP:
        candidates.append(HASHTAG_SEGMENTATION_MAP[norm_q])
    if alternative_queries:
        for alt in alternative_queries:
            if alt and alt not in candidates:
                candidates.append(alt)

    title = tokens(result.title or "")
    caption = tokens(result.caption or "")
    expanded_tags_list: list[str] = []
    for t in (result.hashtags or []):
        raw_t = str(t)
        expanded_tags_list.append(raw_t)
        norm_t = normalize_hashtag_str(raw_t)
        expanded_tags_list.append(norm_t)
        canonical = CANONICAL_HASHTAG_MAP.get(norm_t)
        if canonical:
            expanded_tags_list.append(canonical)
            if canonical in HASHTAG_SEGMENTATION_MAP:
                expanded_tags_list.append(HASHTAG_SEGMENTATION_MAP[canonical])
        elif norm_t in HASHTAG_SEGMENTATION_MAP:
            expanded_tags_list.append(HASHTAG_SEGMENTATION_MAP[norm_t])
    tags = tokens(" ".join(expanded_tags_list))
    target_tokens = title | caption | tags

    best_relevance = 0.0
    best_cand = candidates[0]
    best_matched_tokens: list[str] = []
    best_missing_core: list[str] = []
    best_provenance: dict[str, list[str]] = {}

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

                # Chống spam mô tả: nếu từ khóa cốt lõi chỉ xuất hiện sâu trong caption dài (>50 ký tự)
                # mà hoàn toàn không có trong tiêu đề hoặc hashtag, áp mức trần liên quan
                if not (core_matched & (title | tags)) and len(result.caption or "") > 50:
                    rel = min(rel, 35.0)
        else:
            coverage = len(matched) / max(len(wanted), 1)
            title_match = len(wanted & title) / max(len(wanted), 1)
            rel = min(100.0, 80 * coverage + 20 * title_match)

        if rel > best_relevance:
            best_relevance = rel
            best_cand = cand
            best_matched_tokens = sorted(matched)
            best_missing_core = sorted(core_wanted - core_matched)
            best_provenance = {
                "title": sorted(matched & title),
                "caption": sorted(matched & caption),
                "hashtags": sorted(matched & tags),
            }

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
    evidence = [result.title or "", result.caption or "", *expanded_tags_list]
    missing_mandatory: list[str] = []
    for name, aliases, ceiling in intent_constraints(query):
        if not any(phrase_matches(part, alias) for part in evidence for alias in aliases):
            relevance = min(relevance, ceiling)
            best_missing_core.append(name)
            missing_mandatory.append(name)

    # Negative keyword penalty
    matched_negatives: list[str] = []
    if negative_keywords:
        for neg in negative_keywords:
            if neg and neg.strip():
                neg_norm = neg.strip()
                if any(phrase_matches(part, neg_norm) for part in evidence):
                    matched_negatives.append(neg_norm)

    if matched_negatives:
        relevance = min(15.0, relevance * 0.25)

    # Hashtag precision matching & scoring
    raw_tags = re.findall(r"#([^\s#]+)", query)
    is_hashtag_search = exact_hashtag or bool(raw_tags) or query.strip().startswith("#")
    active_target_tag = target_hashtag or (
        raw_tags[0]
        if raw_tags
        else (query.strip().lstrip("#").split()[0] if query.strip().startswith("#") else None)
    )

    video_raw_hashtags = result.hashtags or []
    video_clean_tags: list[str] = [
        str(h).strip().lstrip("#") for h in video_raw_hashtags if str(h).strip()
    ]

    hashtag_matched = False
    matched_hashtag_names: list[str] = []

    if active_target_tag:
        norm_target = normalize_hashtag_str(active_target_tag)
        target_canonical = CANONICAL_HASHTAG_MAP.get(norm_target, norm_target)

        for vt in video_clean_tags:
            nvt = normalize_hashtag_str(vt)
            vt_canonical = CANONICAL_HASHTAG_MAP.get(nvt, nvt)

            # Strict equality after normalization or declared alias match ONLY
            if norm_target == nvt or (
                target_canonical == vt_canonical and target_canonical in HASHTAG_ALIAS_MAP
            ):
                hashtag_matched = True
                matched_hashtag_names.append(vt)
    elif video_clean_tags:
        query_core = tokens(query) - GENERIC_MODIFIERS
        for vt in video_clean_tags:
            nvt = normalize_hashtag_str(vt)
            segmented_vt = HASHTAG_SEGMENTATION_MAP.get(nvt, vt)
            vt_toks = tokens(segmented_vt) | tokens(vt)
            if vt_toks and (vt_toks & query_core):
                hashtag_matched = True
                matched_hashtag_names.append(vt)

    if hashtag_matched:
        hashtag_boost = 25.0 if is_hashtag_search else 15.0
        relevance = min(100.0, relevance + hashtag_boost)
        if "hashtags" not in best_provenance:
            best_provenance["hashtags"] = []
        best_provenance["hashtags"] = sorted(list(set(best_provenance["hashtags"] + matched_hashtag_names)))
    elif is_hashtag_search and active_target_tag:
        relevance = min(20.0, relevance * 0.3)
        missing_mandatory.append(f"#{active_target_tag}")
        best_missing_core.append(f"#{active_target_tag}")

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

    from backend.services.virality_service import compute_virality
    virality = compute_virality(result)

    quality = 100 * (0.60 * engagement + 0.15 * freshness + 0.25 * (virality.virality_score / 100.0))
    final_score = relevance * (0.8 + 0.2 * quality / 100)

    return RankBreakdown(
        relevance=round(relevance, 2),
        quality=round(quality, 2),
        final_score=round(final_score, 2),
        virality_score=virality.virality_score,
        velocity_score=virality.velocity_score,
        matched_query=best_cand,
        matched_tokens=best_matched_tokens,
        missing_core_tokens=best_missing_core,
        missing_mandatory_intents=missing_mandatory,
        provenance=best_provenance,
        matched_negative_tokens=matched_negatives,
    )


def rank(
    result: VideoResult,
    query: str,
    alternative_queries: list[str] | None = None,
    negative_keywords: list[str] | None = None,
    exact_hashtag: bool = False,
    target_hashtag: str | None = None,
) -> tuple[float, float, float]:
    detail = rank_detailed(
        result,
        query,
        alternative_queries=alternative_queries,
        negative_keywords=negative_keywords,
        exact_hashtag=exact_hashtag,
        target_hashtag=target_hashtag,
    )
    return detail.relevance, detail.quality, detail.final_score


def recalculate_scores_from_virality(
    relevance: float,
    like_count: int | None = None,
    comment_count: int | None = None,
    favorite_count: int | None = None,
    share_count: int | None = None,
    published_at: datetime | None = None,
    virality_score: float = 0.0,
) -> tuple[float, float]:
    """Recalculates quality_score and final_score given an updated virality_score and engagement."""
    metrics = [
        (like_count, 100000),
        (comment_count, 5000),
        (favorite_count, 20000),
        (share_count, 10000),
    ]
    known = [
        min(1, math.log1p(value) / math.log1p(cap)) for value, cap in metrics if value is not None
    ]
    engagement = sum(known) / len(known) if known else 0
    freshness = 0.0
    if published_at:
        pub = (
            published_at.replace(tzinfo=timezone.utc)
            if published_at.tzinfo is None
            else published_at
        )
        days = max(0, (datetime.now(timezone.utc) - pub).days)
        freshness = math.exp(-days / 60)
    quality = 100.0 * (0.60 * engagement + 0.15 * freshness + 0.25 * (virality_score / 100.0))
    final_score = relevance * (0.8 + 0.2 * quality / 100.0)
    return round(quality, 2), round(final_score, 2)


def cosine_similarity(v1: list[float], v2: list[float]) -> float:
    """Calculates cosine similarity between two numeric vectors in [-1.0, 1.0]."""
    if not v1 or not v2 or len(v1) != len(v2):
        return 0.0
    dot_product = sum(a * b for a, b in zip(v1, v2))
    norm_a = math.sqrt(sum(a * a for a in v1))
    norm_b = math.sqrt(sum(b * b for b in v2))
    if norm_a == 0.0 or norm_b == 0.0:
        return 0.0
    return max(0.0, min(1.0, dot_product / (norm_a * norm_b)))


def compute_hybrid_score(
    lexical_relevance: float,
    semantic_similarity: float | None = None,
    quality_score: float = 0.0,
    semantic_weight: float = 0.5,
    lexical_weight: float = 0.3,
    quality_weight: float = 0.2,
) -> float:
    """
    Computes hybrid ranking score.
    If semantic_similarity is present, blends semantic + lexical + quality.
    Otherwise falls back gracefully to standard lexical ranking.
    """
    if semantic_similarity is not None and 0.0 <= semantic_similarity <= 1.0:
        norm_sem = semantic_similarity * 100.0
        hybrid = (
            semantic_weight * norm_sem
            + lexical_weight * lexical_relevance
            + quality_weight * quality_score
        )
        return round(hybrid, 2)
    # Standard formula fallback
    return round(lexical_relevance * (0.8 + 0.2 * quality_score / 100.0), 2)


STICKER_TWEEZER_TOOL_CJK = ("镊子", "鑷子")
STICKER_TWEEZER_TOOL_LATIN = (
    "nhíp",
    "nhip",
    "nhíp gắp",
    "nhip gap",
    "kẹp gắp",
    "kep gap",
    "tweezers",
    "tweezer",
)

STICKER_TWEEZER_PURPOSE_CJK = ("贴纸", "貼紙", "咕卡")
STICKER_TWEEZER_PURPOSE_LATIN = (
    "sticker",
    "stickers",
    "hình dán",
    "hinh dan",
    "guka",
)


def check_controlled_phrase_match(
    text: str, cjk_phrases: tuple[str, ...], latin_phrases: tuple[str, ...]
) -> bool:
    """Checks for CJK substring presence or bounded whole-word Latin presence in text.

    Rejects loose substring matching (e.g. 'gap' inside 'background' or English words).
    """
    for cjk in cjk_phrases:
        if cjk in text:
            return True
    norm_text = normalize_vietnamese(text).lower()
    for lat in latin_phrases:
        norm_lat = normalize_vietnamese(lat).lower()
        if re.search(r"(?<!\w)" + re.escape(norm_lat) + r"(?!\w)", norm_text):
            return True
    return False


def validate_compound_intent_relevance(
    result: VideoResult,
    breakdown: RankBreakdown,
    query: str = "nhíp gắp sticker",
) -> tuple[bool, str]:
    """Strictly evaluates whether a video candidate satisfies the compound intent of a dual-entity query.

    Rules for 'nhíp gắp sticker':
    1. Media type must be 'video' (photo carousels/notes rejected).
    2. Tool intent (nhíp) must match controlled CJK or whole-word Latin aliases.
    3. Purpose intent (sticker) must match controlled CJK or whole-word Latin aliases.
       NOTE: Standalone '手账' (journal/planner) does NOT count as sticker.
    4. missing_mandatory_intents from breakdown must be clear of tool or purpose.
    5. final_score must meet the floor threshold (>= 15.0).
       NOTE: High score cannot substitute for missing mandatory intent.
    """
    if result.media_type != "video":
        return False, f"Rejected: media_type is '{result.media_type}', expected 'video'"

    combined_text = (
        f"{result.title or ''} {result.caption or ''} {' '.join(result.hashtags or [])}"
    )

    has_tool = check_controlled_phrase_match(
        combined_text, STICKER_TWEEZER_TOOL_CJK, STICKER_TWEEZER_TOOL_LATIN
    )
    has_purpose = check_controlled_phrase_match(
        combined_text, STICKER_TWEEZER_PURPOSE_CJK, STICKER_TWEEZER_PURPOSE_LATIN
    )

    if not has_tool and not has_purpose:
        return (
            False,
            "Rejected: Missing both tool (tweezer/nhíp) and purpose (sticker) intents",
        )
    if not has_tool:
        return (
            False,
            "Rejected: Missing mandatory tool intent (tweezer/nhíp). Contains sticker/stationery only",
        )
    if not has_purpose:
        return (
            False,
            "Rejected: Missing mandatory purpose intent (sticker/guka). Contains tweezer for unrelated use (e.g. eyelash/repair/planner only)",
        )

    # Check breakdown missing mandatory intents: if ranker flagged missing core or mandatory intent
    if any(
        m in ("nhip", "sticker", "镊子", "贴纸")
        for m in breakdown.missing_mandatory_intents
    ):
        return (
            False,
            f"Rejected: Ranker identified missing mandatory intents: {breakdown.missing_mandatory_intents}",
        )

    if breakdown.final_score < 15.0:
        return (
            False,
            f"Rejected: final_score {breakdown.final_score:.1f} below minimum floor threshold 15.0",
        )

    return (
        True,
        "Relevant: Satisfies both tool (tweezer) and purpose (sticker) intents with video media evidence",
    )
