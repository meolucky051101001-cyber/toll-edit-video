import math
import re
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

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
            action_aliases = [*equivalents, *aliases]
            if name == "decor" and re.search(r"\bnha\b", norm):
                # Vietnamese home-decor intent is searched in Chinese. Keep the
                # aliases specific to house/interior design so furniture-sale
                # posts do not pass merely because they mention 家具.
                action_aliases.extend(
                    [
                        "家居装饰",
                        "家居布置",
                        "家装",
                        "房间布置",
                        "房间装饰",
                        "室内装饰",
                        "室内设计",
                        "装修",
                        "装修设计",
                        "软装",
                    ]
                )
            groups.append((name, action_aliases, 45.0))
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

    # The strict sticker-tweezer intent is a video request. Keep the ranker
    # consistent with the production validator so photo notes cannot outrank
    # verified videos just because their title has both words.
    if normalize_vietnamese(query).strip() == "nhip gap sticker" and result.media_type != "video":
        relevance = 0.0
        missing_mandatory.append("video")

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


def validate_live_gate_config(
    min_samples: Any, min_relevance_rate: Any
) -> tuple[int, float]:
    """Validates live gate acceptance thresholds.

    Rules:
    - min_samples must be integer >= 3.
    - min_relevance_rate must be float with 0.0 < rate <= 1.0 (not NaN, not infinite).

    Raises ValueError with descriptive explanation if any boundary is violated.
    """
    try:
        samples_val = int(min_samples)
    except (TypeError, ValueError) as err:
        raise ValueError(
            f"Invalid MIN_LIVE_SAMPLES={min_samples!r}: must be an integer >= 3"
        ) from err

    if samples_val < 3:
        raise ValueError(
            f"Invalid MIN_LIVE_SAMPLES={samples_val}: minimum allowed sample size for live gate is 3"
        )

    try:
        rate_val = float(min_relevance_rate)
    except (TypeError, ValueError) as err:
        raise ValueError(
            f"Invalid MIN_LIVE_RELEVANCE_RATE={min_relevance_rate!r}: must be a float in (0.0, 1.0]"
        ) from err

    if math.isnan(rate_val) or math.isinf(rate_val):
        raise ValueError(
            f"Invalid MIN_LIVE_RELEVANCE_RATE={rate_val}: cannot be NaN or infinite"
        )

    if rate_val <= 0.0 or rate_val > 1.0:
        raise ValueError(
            f"Invalid MIN_LIVE_RELEVANCE_RATE={rate_val}: must be strictly greater than 0.0 and <= 1.0"
        )

    return samples_val, rate_val


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


def validate_sticker_tweezer_compound_intent(
    result: VideoResult,
    breakdown: RankBreakdown,
) -> tuple[bool, str]:
    """Strictly evaluates whether a video candidate satisfies the compound intent of 'nhíp gắp sticker'.

    Rules:
    1. Media type must be 'video' (photo carousels/notes rejected).
    2. Video title must NOT be off-topic. Both intent groups (Tool: tweezer/nhíp AND Purpose: sticker/咕卡)
       must be supported by the title. A title having only one intent cannot rely on caption or hashtags
       to supplement the missing intent. Hashtags are strictly secondary signals and cannot satisfy
       a mandatory intent.
    3. Incompatible context check: Any incompatible domain context (e.g. eyelash extension '睫毛'/'nối mi',
       phone/electronics repair '修手机'/'sửa điện thoại', watch repair '修表', soldering '焊点') in title
       OR caption causes immediate rejection.
    4. Standalone '手账' (planner/journal) without explicit sticker/guka keywords does NOT count as sticker.
    5. Rank breakdown missing_mandatory_intents must be clean of core intents.
    6. final_score must meet the floor threshold (>= 15.0). High score cannot substitute for missing intent.

    SCOPE LIMITATION:
    This validator evaluates textual title and caption/hashtag metadata only; it does not assert
    multimodal visual frame understanding or video OCR comprehension.
    """
    if result.media_type != "video":
        return False, f"Rejected: media_type is '{result.media_type}', expected 'video'"

    title_text = (result.title or "").strip()
    caption_text = (result.caption or "").strip()

    # 1. Incompatible domain rejection in title OR caption
    incompatible_cjk = ("睫毛", "修手机", "手机维修", "焊点", "电路板", "修表", "双眼皮", "美甲")
    incompatible_latin = ("eyelash", "noi mi", "sua dien thoai", "sua chua", "soldering", "nail")
    if check_controlled_phrase_match(title_text, incompatible_cjk, incompatible_latin):
        return (
            False,
            "Rejected: Incompatible purpose detected in title (e.g. eyelash extension or electronics repair tweezers)",
        )
    if check_controlled_phrase_match(caption_text, incompatible_cjk, incompatible_latin):
        return (
            False,
            "Rejected: Incompatible purpose detected in caption (e.g. eyelash extension or electronics repair tweezers)",
        )

    # 2. Strict Title intent verification: Title must contain BOTH tool and purpose intents
    title_has_tool = check_controlled_phrase_match(
        title_text, STICKER_TWEEZER_TOOL_CJK, STICKER_TWEEZER_TOOL_LATIN
    )
    title_has_purpose = check_controlled_phrase_match(
        title_text, STICKER_TWEEZER_PURPOSE_CJK, STICKER_TWEEZER_PURPOSE_LATIN
    )

    if not title_has_tool and not title_has_purpose:
        return (
            False,
            "Rejected: Video title has no match for tool or purpose intent (off-topic title; keywords in caption/hashtag cannot substitute for title)",
        )
    if not title_has_tool:
        return (
            False,
            "Rejected: Video title matches purpose intent but lacks tool intent (tweezer/nhíp); caption/hashtag cannot supply missing tool intent",
        )
    if not title_has_purpose:
        return (
            False,
            "Rejected: Video title matches tool intent but lacks purpose intent (sticker); caption/hashtag cannot supply missing purpose intent",
        )

    # 3. Check breakdown missing mandatory intents
    if any(
        m in ("nhip", "sticker", "镊子", "贴纸")
        for m in breakdown.missing_mandatory_intents
    ):
        return (
            False,
            f"Rejected: Ranker identified missing mandatory intents: {breakdown.missing_mandatory_intents}",
        )

    # 4. Floor score threshold
    if breakdown.final_score < 15.0:
        return (
            False,
            f"Rejected: final_score {breakdown.final_score:.1f} below minimum floor threshold 15.0",
        )

    return (
        True,
        "Relevant: Satisfies both tool (tweezer) and purpose (sticker) intents in title with video media evidence",
    )


def validate_compound_intent_relevance(
    result: VideoResult,
    breakdown: RankBreakdown,
    query: str = "nhíp gắp sticker",
) -> tuple[bool, str]:
    """Validates compound intent for supported dual-entity queries.

    Currently strictly scoped to 'nhíp gắp sticker' (sticker tweezers).
    Raises ValueError if invoked with an unsupported query.
    """
    norm_q = normalize_vietnamese(query).strip().lower()
    if norm_q not in ("nhip gap sticker", "nhíp gắp sticker"):
        raise ValueError(
            f"validate_compound_intent_relevance is strictly scoped to 'nhíp gắp sticker', "
            f"received unsupported query: '{query}'"
        )
    return validate_sticker_tweezer_compound_intent(result, breakdown)
