"""Resource-bounded batching that is independent of media duration."""

from __future__ import annotations

from typing import Any, Callable, Iterable, List, Sequence


def chunked(items: Sequence[Any], size: int) -> List[List[Any]]:
    if size <= 0:
        raise ValueError("size must be positive")
    return [list(items[index : index + size]) for index in range(0, len(items), size)]


def bounded_batches(
    items: Iterable[Any],
    max_items: int,
    max_weight: int,
    weight: Callable[[Any], int],
) -> List[List[Any]]:
    """Split by both item count and payload weight without dropping order."""

    if max_items <= 0 or max_weight <= 0:
        raise ValueError("Batch limits must be positive")
    batches: List[List[Any]] = []
    current: List[Any] = []
    current_weight = 0
    for item in items:
        item_weight = max(1, int(weight(item)))
        if current and (
            len(current) >= max_items or current_weight + item_weight > max_weight
        ):
            batches.append(current)
            current = []
            current_weight = 0
        current.append(item)
        current_weight += item_weight
    if current:
        batches.append(current)
    return batches
