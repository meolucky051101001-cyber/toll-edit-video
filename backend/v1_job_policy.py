"""Pure, per-job model selection. Auto voice and video mode are independent."""
from copy import deepcopy


def capture_job_settings(workspace):
    """Capture settings when a job is queued; no credentials or model loading."""
    from v1_feature_flags import get_feature_flags
    from ai.v1_auto_voice import get_auto_voice_config
    import voice_selection
    voice = deepcopy(get_auto_voice_config(str(workspace)))
    voice["manual_voice"] = dict(voice_selection.selected())
    try:
        from audio_settings import get_audio_settings
        script_mode = get_audio_settings().get("script_mode", "default")
    except Exception:
        script_mode = "default"
    return {"feature_flags": deepcopy(get_feature_flags(workspace)),
            "voice_config": voice,
            "voice_mode": "auto" if voice.get("enabled") else "manual",
            "script_mode": script_mode}

ASR_ALIASES = {
    "whisper_turbo": "whisper_turbo", "whisper_large_v3_turbo": "whisper_turbo",
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
        asr = flags.get("V1_ASR_MODEL", "whisper_turbo")
    if asr not in ASR_ALIASES:
        raise ValueError(f"Unsupported V1 ASR model: {asr}")
    pipeline.update(
        separation_model=SEPARATOR_ALIASES[separator], separator_engine=SEPARATOR_ALIASES[separator],
        asr_model=ASR_ALIASES[asr],
        asr_chunking=bool(flags.get("V1_ASR_CHUNKING", True)) and profile == "LONG",
        asr_chunk_size_s=240.0, asr_overlap_s=0.75,
        ocr_strategy="smart_skip" if profile == "LONG" and flags.get("V1_SMART_SKIP_OCR", True) else "full",
        mixer_mode="direct" if profile == "SHORT" else "hierarchical",
    )
    if 0 < duration <= threshold:
        pipeline["ocr_strategy"] = "full"
    result["policy_version"] = 2
    return result
