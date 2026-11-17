"""Refine sampled caption changes to adjacent source frames, without providers."""
from __future__ import annotations

import math


def refine_observations(observations, frame_count, observe, signature, stopped=lambda: False,
                        max_extra_frames=4000):
    """Bisect changed intervals; retain empty observations as real boundaries.

    Matching endpoints keep the coarse sampling contract. Different endpoints
    are refined until adjacent frames, including intermediate third captions.
    The budget fails closed rather than labelling an unresolved interval exact.
    """
    samples = dict(observations)
    extra = 0

    def sample(index):
        nonlocal extra
        if index not in samples:
            if stopped():
                raise RuntimeError("OCR stop requested")
            if extra >= max_extra_frames:
                raise RuntimeError("Caption boundary refinement exceeded its frame budget")
            samples[index] = observe(index)
            extra += 1
        return samples[index]

    if frame_count <= 0:
        raise ValueError("Caption refinement requires a positive frame count")
    sample(0)
    sample(frame_count - 1)
    ordered = sorted(samples)
    pending = list(zip(ordered, ordered[1:]))
    while pending:
        left, right = pending.pop()
        if right - left <= 1 or signature(samples[left]) == signature(samples[right]):
            continue
        middle = (left + right) // 2
        sample(middle)
        pending.extend(((left, middle), (middle, right)))
    return samples, extra


def observation_intervals(samples, fps, duration):
    """Zero-order hold between verified observations, with half-open times."""
    if not math.isfinite(fps) or fps <= 0:
        raise ValueError("Caption timing requires valid FPS")
    ordered = sorted(samples)
    for position, index in enumerate(ordered):
        end = ordered[position + 1] / fps if position + 1 < len(ordered) else duration
        start = index / fps
        if end > start:
            yield samples[index], start, min(end, duration)
