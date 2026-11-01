"""Failures that require valid state/artifacts before work can continue."""


class JobStateReadError(RuntimeError):
    """The active-job state cannot be verified safely."""


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
