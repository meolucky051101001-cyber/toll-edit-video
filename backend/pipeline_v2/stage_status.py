"""Stage states and their guarded transitions."""

from enum import Enum
from typing import Dict, FrozenSet


class StageStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    SKIPPED = "skipped"

    @property
    def is_terminal(self) -> bool:
        return self in {self.COMPLETED, self.FAILED, self.SKIPPED}


class InvalidStageTransition(ValueError):
    """Raised when code attempts an unsafe stage-state transition."""


_ALLOWED_TRANSITIONS: Dict[StageStatus, FrozenSet[StageStatus]] = {
    StageStatus.PENDING: frozenset({StageStatus.RUNNING, StageStatus.SKIPPED}),
    StageStatus.RUNNING: frozenset({StageStatus.COMPLETED, StageStatus.FAILED}),
    StageStatus.FAILED: frozenset({StageStatus.RUNNING}),
    StageStatus.COMPLETED: frozenset(),
    StageStatus.SKIPPED: frozenset(),
}


def require_transition(current: StageStatus, target: StageStatus) -> None:
    if target not in _ALLOWED_TRANSITIONS[current]:
        raise InvalidStageTransition(
            "Cannot change stage status from {!r} to {!r}".format(
                current.value, target.value
            )
        )
