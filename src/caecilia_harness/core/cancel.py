"""Cooperative cancellation shared between the orchestrator, workers and providers."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class CancelFlag:
    """A one-way flag satisfying `core.ports.CancelToken`. Once cancelled it stays cancelled."""

    _cancelled: bool = field(default=False, init=False)

    @property
    def cancelled(self) -> bool:
        return self._cancelled

    def cancel(self) -> None:
        """Request cancellation. Safe to call more than once."""
        self._cancelled = True
