"""Pure, per-job model selection. Auto voice and video mode are independent."""
from copy import deepcopy
import math
import re

TRANSCRIPT_SOURCE_POLICY_VERSION = 1

try:
    from v1_feature_flags import migrate_legacy_asr_model
except ImportError:
    from .v1_feature_flags import migrate_legacy_asr_model


def merged_speech_coverage(segments, duration_s):
    """Union of ASR speech intervals, clamped to the source timeline."""
    intervals = []
    for seg in segments or []:
        try:
            start = max(0.0, seg.start.total_seconds())
            end = seg.end.total_seconds()
        except Exception:
            continue
        if duration_s > 0:
            end = min(float(duration_s), end)
        if end > start:
            intervals.append((start, end))
    intervals.sort()
    merged = []
    for start, end in intervals:
        if merged and start <= merged[-1][1] + 0.05:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return sum(end - start for start, end in merged)


def looks_like_bgm_hallucination(segments, duration_s):
    """Only absent, invalid or explicitly rejected ASR permits visual fallback.

    Low coverage and short utterances do not prove silence. A spoken sentence
    in a mostly musical clip must not become product-label narration.
    """
    for seg in segments or []:
        if getattr(seg, "asr_hallucination", False):
            continue
        try:
            start, end = seg.start.total_seconds(), seg.end.total_seconds()
        except (AttributeError, TypeError, ValueError, OverflowError):
            continue
        if (str(getattr(seg, "content", "") or "").strip()
                and math.isfinite(start) and math.isfinite(end)
                and 0 <= start < end and (duration_s <= 0 or start < duration_s)):
            return False
    return True


def visual_transcript_candidates(segments):
    """Exclude scene/product labels before selecting a silent-video transcript."""
    def value(obj, name, default=None):
        return obj.get(name, default) if isinstance(obj, dict) else getattr(obj, name, default)

    candidates = []
    for segment in segments or []:
        if not value(segment, "visual_sub_to_speech", False):
            continue
        block = value(segment, "best_block")
        if any(value(item, "is_packaging", False) or value(item, "is_static", False)
               or value(item, "is_subtitle") is False
               or value(item, "in_subtitle_band") is False
               for item in (segment, block) if item is not None):
            continue
        text = str(value(segment, "content", "") or "").strip()
        # Moving product labels can be misclassified as a caption band.
        if re.search(r"生产(?:商|厂家|地址)|执行标准|(?:材质|品名|货号|型号)\s*[|｜:：]", text):
            continue
        candidates.append(segment)
    return candidates


def visual_ocr_transcript_usable(segments, duration_s):
    """A non-product caption timeline usable after speech was rejected."""
    cues = visual_transcript_candidates(segments)
    if not cues:
        return False
    coverage = merged_speech_coverage(cues, duration_s)
    if coverage < 0.8:
        return False
    if len(cues) >= 2:
        return True
    return bool(duration_s > 0 and coverage >= 0.15 * float(duration_s))


def resolve_visual_sub_to_speech(effective_config, workspace=None):
    """Frozen job value wins; never consult global state once it is recorded."""
    config = effective_config or {}
    explicit = config.get("visual_sub_to_speech")
    if explicit is None:
        explicit = (config.get("feature_flags") or {}).get("V1_VISUAL_SUB_TO_SPEECH")
    if explicit is None:
        try:
            from v1_feature_flags import get_feature_flags
            explicit = get_feature_flags(workspace).get("V1_VISUAL_SUB_TO_SPEECH", False)
        except Exception:
            explicit = False
    return bool(explicit)


def capture_job_settings(workspace):
    """Capture settings when a job is queued; no credentials or model loading."""
    from v1_feature_flags import get_feature_flags
    from ai.v1_auto_voice import get_auto_voice_config
    import voice_selection
    flags = deepcopy(get_feature_flags(workspace))
    voice = deepcopy(get_auto_voice_config(str(workspace)))
    voice["manual_voice"] = dict(voice_selection.selected())
    try:
        from audio_settings import get_audio_settings
        script_mode = get_audio_settings().get("script_mode", "default")
    except Exception:
        script_mode = "default"
    return {"feature_flags": flags,
            "visual_sub_to_speech": bool(flags.get("V1_VISUAL_SUB_TO_SPEECH", False)),
            "voice_config": voice,
            "voice_mode": "auto" if voice.get("enabled") else "manual",
            "script_mode": script_mode}

ASR_ALIASES = {
    "whisper_turbo": "whisper_turbo", "whisper_large_v3_turbo": "whisper_turbo",
    "whisper_sensevoice": "whisper_sensevoice", "whisper_sensevoice_hybrid": "whisper_sensevoice", "hybrid": "whisper_sensevoice",
    "sensevoice_small": "sensevoice_small", "sensevoice": "sensevoice_small",
}
SEPARATOR_ALIASES = {
    "roformer": "roformer", "bs_roformer_sdr12": "roformer",
    "demucs": "demucs", "demucs_htdemucs": "demucs",
}


def apply_job_policy(routing, flags, overrides=None):
    """Resolve explicit choices > length profile > existing global ASR choice.

    Resource escalation must not change the audio model for a short 4K clip.
    No process environment or on-disk settings are mutated here.
    """
    result = deepcopy(routing)
    overrides = overrides or {}
    requested = result.get("requested_mode", "AUTO").upper()
    duration = float(result.get("metadata", {}).get("duration_s", 0))
    threshold = float(result.get("thresholds", {}).get("short_max_seconds", 420))
    mode = result["resolved_mode"]
    if requested == "AUTO" and 0 < duration <= threshold:
        mode = result["resolved_mode"] = "SHORT"
    profile = "SHORT" if mode == "SHORT" else "LONG"
    pipeline = result["planned_pipeline"]
    separator = (overrides.get("separation_model") or overrides.get("separation_mode")
                 or flags.get(f"V1_{profile}_SEPARATOR", "roformer" if profile == "SHORT" else "demucs"))
    if separator not in SEPARATOR_ALIASES:
        raise ValueError(f"Unsupported V1 separator: {separator}")
    asr = overrides.get("asr_model") or flags.get(f"V1_{profile}_ASR_MODEL", "inherit")
    if asr == "inherit":
        asr = flags.get("V1_ASR_MODEL", "whisper_sensevoice")
    asr = migrate_legacy_asr_model(asr, "job policy")
    if asr not in ASR_ALIASES:
        raise ValueError(f"Unsupported V1 ASR model: {asr}")
    pipeline.update(
        separation_model=SEPARATOR_ALIASES[separator], separator_engine=SEPARATOR_ALIASES[separator],
        asr_model=ASR_ALIASES[asr],
        asr_chunking=bool(flags.get("V1_ASR_CHUNKING", True)) and profile == "LONG",
        asr_chunk_size_s=240.0, asr_overlap_s=0.75,
        ocr_strategy="smart_skip" if profile == "LONG" and flags.get("V1_SMART_SKIP_OCR", True) else "full",
        mixer_mode="direct" if profile == "SHORT" else "hierarchical",
        visual_sub_to_speech=bool(
            overrides.get("visual_sub_to_speech")
            if overrides.get("visual_sub_to_speech") is not None
            else flags.get("V1_VISUAL_SUB_TO_SPEECH", False)
        ),
    )
    if 0 < duration <= threshold:
        pipeline["ocr_strategy"] = "full"
    result["policy_version"] = 2
    return result
