"""Exceptions raised by worker adapters."""

from __future__ import annotations


class WorkerError(Exception):
    """Base class for worker adapter errors."""


class UnknownRunHandleError(WorkerError, KeyError):
    """The `RunHandle` was not started by this worker instance."""
