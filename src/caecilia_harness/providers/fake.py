"""Deterministic, script-driven `ModelProvider` for tests and offline runs.

Never touches the network.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Sequence
from typing import Any

from caecilia_harness.core.models import ModelCaps, ModelEvent, ModelRequest
from caecilia_harness.core.ports import CancelToken

DEFAULT_CAPS = ModelCaps(
    supports_tools=True,
    supports_streaming=True,
    reports_usage=False,
    context_window=200_000,
    max_output_tokens=8_192,
)

_TERMINAL_KINDS = frozenset({"done", "error"})


class FakeProvider:
    """Replays one scripted turn per `stream()` call.

    A turn that does not end with `done`/`error` gets `done(end_turn)` appended; events after
    a terminal event are dropped. Cancellation is checked before every event.
    """

    id: str = "fake"

    def __init__(
        self,
        script: Sequence[Sequence[ModelEvent]],
        caps: ModelCaps | None = None,
        delay: float = 0.0,
    ) -> None:
        self._script = [list(turn) for turn in script]
        self._caps = caps or DEFAULT_CAPS
        self._delay = delay
        self._next_turn = 0
        self.requests: list[ModelRequest] = []

    # --- script helpers ---------------------------------------------------------

    @staticmethod
    def text(text: str) -> ModelEvent:
        return ModelEvent(kind="text_delta", data={"text": text})

    @staticmethod
    def tool_call(id: str, name: str, input: dict[str, Any]) -> ModelEvent:
        return ModelEvent(kind="tool_call", data={"id": id, "name": name, "input": input})

    @staticmethod
    def usage(input_tokens: int, output_tokens: int) -> ModelEvent:
        return ModelEvent(
            kind="usage", data={"input_tokens": input_tokens, "output_tokens": output_tokens}
        )

    # --- ModelProvider ------------------------------------------------------------

    def capabilities(self, model: str) -> ModelCaps:
        return self._caps

    def stream(self, req: ModelRequest, cancel: CancelToken) -> AsyncIterator[ModelEvent]:
        """Record `req` and consume the next turn immediately, then stream it lazily."""
        self.requests.append(req)
        if self._next_turn >= len(self._script):
            turn = [
                ModelEvent(
                    kind="error",
                    data={
                        "message": f"FakeProvider script exhausted after "
                        f"{len(self._script)} turn(s)",
                        "retryable": False,
                    },
                )
            ]
        else:
            turn = self._script[self._next_turn]
            self._next_turn += 1
        return self._replay(turn, cancel)

    async def _replay(
        self, turn: list[ModelEvent], cancel: CancelToken
    ) -> AsyncIterator[ModelEvent]:
        if not turn or turn[-1].kind not in _TERMINAL_KINDS:
            turn = [*turn, ModelEvent(kind="done", data={"stop_reason": "end_turn"})]
        for event in turn:
            if self._delay:
                await asyncio.sleep(self._delay)
            if cancel.cancelled:
                yield ModelEvent(kind="done", data={"stop_reason": "cancelled"})
                return
            yield event
            if event.kind in _TERMINAL_KINDS:
                return
