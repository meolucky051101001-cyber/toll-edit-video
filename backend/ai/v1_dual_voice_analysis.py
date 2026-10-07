"""Conservative per-cue voice routing; no majority-gender correction or new model."""
from pathlib import Path
import math
import numpy as np

ANALYSIS_VERSION = 'dual_voice_evidence_1'


def _unknown(reason, **extra):
    return dict(gender='unknown', confidence=0.0, reason=reason, **extra)


def _audio_evidence(audio, sample_rate, estimate):
    y = np.asarray(audio, dtype=np.float64)
    if sample_rate <= 0 or y.ndim != 1 or len(y) < sample_rate * .25:
        return _unknown('too_short')
    if not np.isfinite(y).all() or np.max(np.abs(y)) < 1e-5:
        return _unknown('silent_or_invalid')
    # At most four overlapping windows, rather than one median for two speakers.
    size = min(len(y), max(int(sample_rate * .6), min(int(sample_rate * 1.2), len(y))))
    starts = np.unique(np.linspace(0, len(y) - size, min(4, 1 + math.ceil((len(y) - size) / size)), dtype=int))
    votes = []
    for start in starts:
        f0, count, confidence, gender, hnr = estimate(y[start:start + size], sample_rate)
        if not all(math.isfinite(float(v)) for v in (f0, confidence, hnr)):
            continue
        # HNR gates noisy/periodic fallback. Confidence here is a heuristic score,
        # not a calibrated probability of a person's biological sex.
        if count < 12 or confidence < .70 or hnr < 3 or gender not in ('male', 'female'):
            continue
        if (gender == 'male' and 75 <= f0 < 175) or (gender == 'female' and 185 <= f0 <= 340):
            votes.append((gender, float(confidence), float(f0)))
    if not votes:
        return _unknown('insufficient_voiced_evidence', windows=len(starts))
    genders = {v[0] for v in votes}
    # Opposite reliable votes mean a mixed cue or pitch error, not "majority wins".
    if len(genders) != 1:
        return _unknown('mixed_or_unstable_pitch', windows=len(starts))
    if len(votes) / len(starts) < .5:
        return _unknown('too_few_reliable_windows', windows=len(starts))
    pitches = np.array([v[2] for v in votes])
    if len(pitches) > 1 and pitches.max() / pitches.min() > 1.6:
        return _unknown('unstable_pitch', windows=len(starts))
    return dict(gender=votes[0][0], confidence=min(v[1] for v in votes),
                median_f0=float(np.median(pitches)), reason='consistent_voiced_windows',
                windows=len(starts), reliable_windows=len(votes))


def classify_dialogue(segments, vocals_path, original_path, *, read_slice, estimate,
                      get_start, get_end, get_index, get_content):
    """Return {cue_id: evidence}. Never smooth away a minority speaker's cue."""
    paths = []
    for label, path in (('vocals', vocals_path), ('original', original_path)):
        if path and Path(path).is_file():
            resolved = str(Path(path).resolve())
            if resolved not in [p for _, p in paths]:
                paths.append((label, resolved))
    result = {}
    for position, seg in enumerate(segments, 1):
        index = get_index(seg, position)
        if index in result:
            raise ValueError(f'Duplicate subtitle cue ID {index}; cannot route voices safely')
        start, end = get_start(seg), get_end(seg)
        if not get_content(seg).strip() or not math.isfinite(start) or not math.isfinite(end) or start < 0 or end <= start:
            result[index] = _unknown('invalid_or_empty_cue')
            continue
        sources = {}
        for label, path in paths:
            # Bound analysis time/memory without decoding an entire long video.
            sample = read_slice(path, start, min(end, start + 12.0))
            if sample is None:
                sources[label] = _unknown('audio_slice_unavailable')
            else:
                sources[label] = _audio_evidence(sample[0], sample[1], estimate)
        reliable = [e for e in sources.values() if e['gender'] != 'unknown']
        if not reliable:
            chosen = _unknown('no_reliable_source')
        elif len({e['gender'] for e in reliable}) > 1:
            chosen = _unknown('vocals_original_disagree')
        elif any(e['reason'] == 'mixed_or_unstable_pitch' for e in sources.values()):
            chosen = _unknown('mixed_speaker_cue')
        else:
            chosen = dict(min(reliable, key=lambda e: e['confidence']))
            chosen['reason'] = 'sources_agree' if len(reliable) > 1 else 'one_reliable_source'
        chosen['sources'] = sources
        chosen['analysis_version'] = ANALYSIS_VERSION
        result[index] = chosen
    return result
