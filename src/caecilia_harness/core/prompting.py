"""Ask the person one question at a time (ADR-0013 point 7).

With several workers running, two of them may need an approval at once, or a merge confirmation
may arrive while an approval is open. Questions must never interleave on the screen, and waiting
for an answer must not block the event loop (the wrapped prompter is responsible for that part).
"""

from __future__ import annotations

import asyncio

from caecilia_harness.core.models import Action, GateReport
from caecilia_harness.core.ports import UserPrompter


class SerializedPrompter:
    """`UserPrompter` that lets one question through at a time, in arrival order.

    `asyncio.Lock` hands itself to waiters first-in, first-out, so questions are answered in the
    order they were asked.
    """

    def __init__(self, inner: UserPrompter) -> None:
        self.inner = inner
        self._lock = asyncio.Lock()

    async def ask_approval(self, action: Action) -> bool:
        async with self._lock:
            return await self.inner.ask_approval(action)

    async def confirm_merge(self, report: GateReport) -> bool:
        async with self._lock:
            return await self.inner.confirm_merge(report)
