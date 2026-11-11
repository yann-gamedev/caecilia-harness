"""Contract every `ModelProvider` must pass (docs/architecture.md, "Kontrak `ModelEvent`").

Subclass `ModelProviderContract` in a `test_*.py` file and provide two fixtures:

- `provider`: a fresh provider, ready for at least one `stream()` call;
- `simple_request`: a `ModelRequest` that the provider answers normally.
"""

from __future__ import annotations

from typing import Any

from caecilia_harness.core.cancel import CancelFlag
from caecilia_harness.core.models import ModelEvent, ModelRequest
from caecilia_harness.core.ports import ModelProvider

TERMINAL_KINDS = frozenset({"done", "error"})
STOP_REASONS = frozenset({"end_turn", "tool_use", "max_tokens", "cancelled"})

#: kind -> {required data key: accepted type(s)}
REQUIRED_DATA: dict[str, dict[str, type | tuple[type, ...]]] = {
    "text_delta": {"text": str},
    "tool_call": {"id": str, "name": str, "input": dict},
    "usage": {"input_tokens": int, "output_tokens": int},
    "done": {"stop_reason": str},
    "error": {"message": str, "retryable": bool},
}


async def collect(
    provider: ModelProvider, request: ModelRequest, cancel: CancelFlag | None = None
) -> list[ModelEvent]:
    """Drain one stream into a list."""
    return [event async for event in provider.stream(request, cancel or CancelFlag())]


def assert_single_terminal_at_end(events: list[ModelEvent]) -> None:
    terminals = [i for i, event in enumerate(events) if event.kind in TERMINAL_KINDS]
    assert terminals == [len(events) - 1], [e.kind for e in events]


class ModelProviderContract:
    """Base class of the provider contract suite. Not collected on its own (no `Test` prefix)."""

    async def test_is_a_model_provider(self, provider: Any) -> None:
        assert isinstance(provider, ModelProvider)

    async def test_stream_ends_with_exactly_one_terminal_event(
        self, provider: ModelProvider, simple_request: ModelRequest
    ) -> None:
        events = await collect(provider, simple_request)
        assert events, "stream yielded nothing"
        assert_single_terminal_at_end(events)

    async def test_events_follow_the_data_contract(
        self, provider: ModelProvider, simple_request: ModelRequest
    ) -> None:
        for event in await collect(provider, simple_request):
            required = REQUIRED_DATA[event.kind]
            for key, expected_type in required.items():
                assert key in event.data, f"{event.kind} is missing {key!r}"
                assert isinstance(event.data[key], expected_type), (event.kind, key)
            if event.kind == "done":
                assert event.data["stop_reason"] in STOP_REASONS

    async def test_cancel_before_completion_yields_cancelled_done(
        self, provider: ModelProvider, simple_request: ModelRequest
    ) -> None:
        cancel = CancelFlag()
        cancel.cancel()
        events = await collect(provider, simple_request, cancel)
        assert_single_terminal_at_end(events)
        assert events[-1].kind == "done"
        assert events[-1].data["stop_reason"] == "cancelled"

    async def test_reports_usage_when_it_says_so(
        self, provider: ModelProvider, simple_request: ModelRequest
    ) -> None:
        if not provider.capabilities(simple_request.model).reports_usage:
            return
        events = await collect(provider, simple_request)
        assert any(event.kind == "usage" for event in events)
