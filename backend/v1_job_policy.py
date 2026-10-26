"""Pure, per-job model selection. Auto voice and video mode are independent."""
from copy import deepcopy


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
    """ASR output too sparse/short to be a real voice track (music-only clips)."""
    if not segments:
        return True
    durations = []
    for seg in segments:
        try:
            durations.append(max(0.0, seg.end.total_seconds() - seg.start.total_seconds()))
        except Exception:
            continue
    if len(segments) <= 2 and max(durations, default=0.0) <= 1.5:
        return True
    # Matches perform_video_ocr's visual-mode gate so sparse ASR cannot be
    # mixed with recovered on-screen cues (which would duplicate dialogue).
    if duration_s > 0 and merged_speech_coverage(segments, duration_s) < 0.25 * duration_s:
        return True
    return False


def visual_ocr_transcript_usable(segments, duration_s):
    """A recovered on-screen caption timeline worth using instead of ASR."""
    cues = [seg for seg in segments or [] if getattr(seg, "visual_sub_to_speech", False)]
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
    "qwen3_asr": "qwen3_asr", "qwen3_asr_preview": "qwen3_asr",
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
