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
