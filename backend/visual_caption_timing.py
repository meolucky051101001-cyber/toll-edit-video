"""Refine sampled caption changes to adjacent source frames, without providers."""
from __future__ import annotations

import math


def refine_observations(observations, frame_count, observe, signature, stopped=lambda: False,
                        max_extra_frames=4000, max_stable_gap_frames=None, *, observe_batch=None):
    """Bisect changed intervals; retain empty observations as real boundaries.

    Matching endpoints keep the coarse sampling contract unless a maximum gap
    is supplied (equal endpoints can hide a short caption or empty interval).
    Different endpoints are refined until adjacent frames, including
    intermediate third captions. A batch observer receives the unknown frame
    indices for one subdivision round and returns observations in index order.
    The budget fails closed rather than labelling an unresolved interval exact.
    """
    samples = dict(observations)
    extra = 0

    def sample(indices):
        nonlocal extra
        indices = sorted(set(indices).difference(samples))
        if not indices:
            return
        if stopped():
            raise RuntimeError("OCR stop requested")
        if extra + len(indices) > max_extra_frames:
            raise RuntimeError("Caption boundary refinement exceeded its frame budget")
        if observe_batch is None:
            for index in indices:
                if stopped():
                    raise RuntimeError("OCR stop requested")
                samples[index] = observe(index)
        else:
            rows = observe_batch(indices)
            if len(rows) != len(indices):
                raise RuntimeError("OCR returned incomplete caption boundary observations")
            samples.update(zip(indices, rows))
        extra += len(indices)

    if frame_count <= 0:
        raise ValueError("Caption refinement requires a positive frame count")
    if max_stable_gap_frames is not None and max_stable_gap_frames < 1:
        raise ValueError("Caption sampling gap must be at least one frame")
    if any(not isinstance(index, int) or not 0 <= index < frame_count for index in samples):
        raise ValueError("Caption observations lie outside the decoded frame range")
    sample((0, frame_count - 1))
    ordered = sorted(samples)
    pending = list(zip(ordered, ordered[1:]))
    signatures = {}

    def at(index):
        if index not in signatures:
            signatures[index] = signature(samples[index])
        return signatures[index]

    while pending:
        middle_indices = []
        children = []
        for left, right in pending:
            gap = right - left
            if gap <= 1:
                continue
            if (max_stable_gap_frames is None or gap <= max_stable_gap_frames) and at(left) == at(right):
                continue
            middle = (left + right) // 2
            middle_indices.append(middle)
            children.extend(((left, middle), (middle, right)))
        sample(middle_indices)
        pending = children
    return samples, extra


def observation_intervals(samples, fps, duration, *, frame_times=None):
    """Hold observations until the next decoded PTS, including VFR sources.

    The FPS fallback is for callers with an explicitly constant frame clock.
    Video acquisition supplies real timestamps; average FPS cannot map VFR
    frame numbers to time.
    """
    if frame_times is None and (fps is None or not math.isfinite(fps) or fps <= 0):
        raise ValueError("Caption timing requires valid FPS or decoded timestamps")
    if not math.isfinite(duration) or duration <= 0:
        raise ValueError("Caption timing requires a positive duration")
    ordered = sorted(samples)
    try:
        times = [float(frame_times[index]) if frame_times is not None else index / fps
                 for index in ordered]
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("Caption observation is missing its decoded timestamp") from exc
    if (any(not math.isfinite(time) or not 0 <= time < duration for time in times)
            or any(right <= left for left, right in zip(times, times[1:]))):
        raise ValueError("Caption timestamps must increase within the source duration")
    for position, index in enumerate(ordered):
        end = times[position + 1] if position + 1 < len(ordered) else duration
        start = times[position]
        if end > start:
            yield samples[index], start, min(end, duration)
