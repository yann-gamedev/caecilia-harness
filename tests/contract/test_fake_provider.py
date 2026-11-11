"""FakeProvider passes the provider contract and behaves as scripted (backlog M1-04)."""

from __future__ import annotations

import asyncio

import pytest

from caecilia_harness.core.cancel import CancelFlag
from caecilia_harness.core.models import Message, ModelCaps, ModelEvent, ModelRequest
from caecilia_harness.core.ports import CancelToken, ModelProvider
from caecilia_harness.providers.fake import FakeProvider
from tests.contract.model_provider_contract import (
    ModelProviderContract,
    assert_single_terminal_at_end,
    collect,
)

CAPS = ModelCaps(
    supports_tools=True,
    supports_streaming=True,
    reports_usage=True,
    context_window=100_000,
    max_output_tokens=4_096,
)


def _request(text: str = "hi") -> ModelRequest:
    return ModelRequest(model="fake-1", messages=[Message(role="user", content=text)])


class TestFakeProviderContract(ModelProviderContract):
    @pytest.fixture
    def provider(self) -> FakeProvider:
        turn = [
            FakeProvider.text("hello"),
            FakeProvider.tool_call("call-1", "read_file", {"path": "README.md"}),
            FakeProvider.usage(12, 5),
            ModelEvent(kind="done", data={"stop_reason": "tool_use"}),
        ]
        return FakeProvider([turn], caps=CAPS)

    @pytest.fixture
    def simple_request(self) -> ModelRequest:
        return _request()


def test_satisfies_ports() -> None:
    assert isinstance(FakeProvider([]), ModelProvider)
    # CancelToken is not runtime_checkable; mypy checks this assignment structurally.
    token: CancelToken = CancelFlag()
    assert token.cancelled is False


def test_cancel_flag_is_one_way() -> None:
    flag = CancelFlag()
    assert not flag.cancelled
    flag.cancel()
    flag.cancel()
    assert flag.cancelled


async def test_turns_are_used_in_order_and_requests_recorded() -> None:
    provider = FakeProvider([[FakeProvider.text("one")], [FakeProvider.text("two")]])
    first = await collect(provider, _request("a"))
    second = await collect(provider, _request("b"))
    assert first[0].data == {"text": "one"}
    assert second[0].data == {"text": "two"}
    assert [r.messages[0].content for r in provider.requests] == ["a", "b"]


async def test_exhausted_script_yields_non_retryable_error() -> None:
    provider = FakeProvider([[FakeProvider.text("only")]])
    await collect(provider, _request())
    events = await collect(provider, _request())
    assert [e.kind for e in events] == ["error"]
    assert events[0].data["retryable"] is False
    assert "exhausted" in events[0].data["message"]
    assert len(provider.requests) == 2


async def test_missing_terminal_gets_end_turn_done() -> None:
    events = await collect(FakeProvider([[FakeProvider.text("x")]]), _request())
    assert [e.kind for e in events] == ["text_delta", "done"]
    assert events[-1].data == {"stop_reason": "end_turn"}


async def test_scripted_error_ends_the_stream() -> None:
    turn = [
        FakeProvider.text("x"),
        ModelEvent(kind="error", data={"message": "rate limited", "retryable": True}),
        FakeProvider.text("never sent"),
    ]
    events = await collect(FakeProvider([turn]), _request())
    assert [e.kind for e in events] == ["text_delta", "error"]


async def test_cancel_mid_stream() -> None:
    turn = [FakeProvider.text(str(i)) for i in range(5)]
    provider = FakeProvider([turn], delay=0.001)
    cancel = CancelFlag()
    received = []
    async for event in provider.stream(_request(), cancel):
        received.append(event)
        if len(received) == 2:
            cancel.cancel()
    assert [e.kind for e in received] == ["text_delta", "text_delta", "done"]
    assert received[-1].data == {"stop_reason": "cancelled"}
    assert_single_terminal_at_end(received)


async def test_delay_is_awaited() -> None:
    provider = FakeProvider([[FakeProvider.text("x")]], delay=0.05)
    loop = asyncio.get_running_loop()
    start = loop.time()
    await collect(provider, _request())
    # Loose bound: the Windows event-loop clock is coarse (~15 ms).
    assert loop.time() - start >= 0.03


def test_default_capabilities() -> None:
    assert FakeProvider([]).capabilities("any").supports_tools
    assert FakeProvider([], caps=CAPS).capabilities("any") == CAPS
