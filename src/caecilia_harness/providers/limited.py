"""Limit concurrent model requests for one shared provider."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator

from caecilia_harness.core.models import ModelCaps, ModelEvent, ModelRequest
from caecilia_harness.core.ports import CancelToken, ModelProvider


class LimitedProvider:
    """Hold a shared semaphore only while consuming an inner request stream."""

    def __init__(self, inner: ModelProvider, semaphore: asyncio.Semaphore) -> None:
        self.id = inner.id
        self._inner = inner
        self._semaphore = semaphore

    def capabilities(self, model: str) -> ModelCaps:
        """Return the inner provider's unchanged capabilities."""
        return self._inner.capabilities(model)

    async def stream(
        self, req: ModelRequest, cancel: CancelToken
    ) -> AsyncGenerator[ModelEvent, None]:
        """Release the slot after completion, failure, cancellation, or explicit close."""
        async with self._semaphore:
            stream = self._inner.stream(req, cancel)
            try:
                async for event in stream:
                    yield event
            finally:
                close = getattr(stream, "aclose", None)
                if close is not None:
                    await close()
