"""Exceptions raised by the store."""

from __future__ import annotations


class StoreError(Exception):
    """Base class for every error raised by the store."""


class MigrationError(StoreError):
    """A schema migration failed and was rolled back."""


class StreamConflictError(StoreError):
    """A CAS append found a different last `stream_seq` than the caller expected."""

    def __init__(self, stream: str, expected: int, actual: int) -> None:
        super().__init__(f"stream {stream!r}: expected stream_seq {expected}, found {actual}")
        self.stream = stream
        self.expected = expected
        self.actual = actual


class ProjectionError(StoreError):
    """An event cannot be applied to the projections (e.g. it refers to an unknown task)."""


class StaleTaskStateError(ProjectionError):
    """A task state change was based on an outdated state or revision."""


class IllegalTransitionError(ProjectionError):
    """A task state change is not allowed by `core.states.TRANSITIONS`."""
