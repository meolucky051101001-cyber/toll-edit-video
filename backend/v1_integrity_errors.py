"""Failures that require valid state/artifacts before work can continue."""


class JobStateReadError(RuntimeError):
    """The active-job state cannot be verified safely."""


class StaleExecutionError(ValueError):
    """A callback no longer owns the execution it is trying to complete."""


class OutputVerificationError(ValueError):
    """An output failed verification; rerendering must remain explicit."""


class CloudRecoveryRequired(ValueError):
    """An existing cloud attempt must be reconciled before queue acknowledgement."""


class PipelineStateError(RuntimeError):
    """A manual job could not establish that the pipeline is available."""


class FrozenConfigError(ValueError):
    """An existing frozen configuration cannot be read or validated."""


class GlossaryConfigError(ValueError):
    """A configured glossary is unreadable or malformed."""


class AudioDurationError(ValueError):
    """No decoder could measure a positive, finite audio duration."""


class SegmentCommitError(RuntimeError):
    """A segment revision could not be committed consistently."""
