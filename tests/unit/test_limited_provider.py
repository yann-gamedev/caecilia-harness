"""Provider slots cover requests and release on every stream exit path."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator, AsyncIterator

import pytest

from caecilia_harness.core.cancel import CancelFlag
from caecilia_harness.core.models import Message, ModelCaps, ModelEvent, ModelRequest
from caecilia_harness.core.ports import CancelToken, ModelProvider
from caecilia_harness.providers.fake import FakeProvider
from caecilia_harness.providers.limited import LimitedProvider
from tests.contract.model_provider_contract import ModelProviderContract, collect


def _request(text: str = "hi") -> ModelRequest:
    return ModelRequest(model="fake-1", messages=[Message(role="user", content=text)])


class _TrackingProvider(FakeProvider):
    def __init__(self) -> None:
        super().__init__([[self.text("hello")]], delay=0.001)
        self.closed = False
        self.block = False
        self.fail = False
        self.close_fails = False
        self.waiting = asyncio.Event()

    async def stream(
        self, req: ModelRequest, cancel: CancelToken
    ) -> AsyncGenerator[ModelEvent, None]:
        try:
            if self.fail:
                raise RuntimeError("request failed")
            async for event in super().stream(req, cancel):
                yield event
                if self.block:
                    self.waiting.set()
                    await asyncio.Event().wait()
        finally:
            self.closed = True
            if self.close_fails:
                raise RuntimeError("close failed")


class TestLimitedProviderContract(ModelProviderContract):
    @pytest.fixture
    def provider(self) -> LimitedProvider:
        caps = ModelCaps(
            supports_tools=True,
            supports_streaming=True,
            reports_usage=True,
            context_window=100_000,
            max_output_tokens=4_096,
        )
        inner = FakeProvider(
            [
                [
                    FakeProvider.text("hello"),
                    FakeProvider.tool_call("c", "read", {}),
                    FakeProvider.usage(12, 5),
                ]
            ],
            caps=caps,
        )
        return LimitedProvider(inner, asyncio.Semaphore(1))

    @pytest.fixture
    def simple_request(self) -> ModelRequest:
        return _request()


def test_delegates_identity_and_capabilities() -> None:
    inner = FakeProvider([])
    inner.id = "custom-provider"
    provider: ModelProvider = LimitedProvider(inner, asyncio.Semaphore(1))
    assert provider.id == inner.id
    assert provider.capabilities("fake-1") is inner.capabilities("fake-1")


async def test_unstarted_stream_neither_reserves_slot_nor_starts_request() -> None:
    inner = FakeProvider([])
    slots = asyncio.Semaphore(1)
    stream = LimitedProvider(inner, slots).stream(_request(), CancelFlag())
    assert slots._value == 1
    assert inner.requests == []
    await stream.aclose()
    assert slots._value == 1
    assert inner.requests == []


async def test_delayed_requests_are_serialized_until_first_stream_finishes() -> None:
    inner = FakeProvider([[FakeProvider.text("one")], [FakeProvider.text("two")]], delay=0.001)
    slots = asyncio.Semaphore(1)
    provider = LimitedProvider(inner, slots)
    first = provider.stream(_request("first"), CancelFlag())
    assert (await anext(first)).data == {"text": "one"}
    second = asyncio.create_task(collect(provider, _request("second")))
    try:
        await asyncio.sleep(0)
        assert not second.done()
        assert [req.messages[0].content for req in inner.requests] == ["first"]
        assert slots._value == 0
        assert [event.kind async for event in first] == ["done"]
        events = await asyncio.wait_for(second, timeout=1)
        assert events[0].data == {"text": "two"}
        assert [req.messages[0].content for req in inner.requests] == ["first", "second"]
        assert slots._value == 1
    finally:
        await first.aclose()
        if not second.done():
            second.cancel()
            with pytest.raises(asyncio.CancelledError):
                await second


async def test_normal_completion_closes_inner_stream() -> None:
    inner = _TrackingProvider()
    slots = asyncio.Semaphore(1)
    events = await collect(LimitedProvider(inner, slots), _request())
    assert [event.kind for event in events] == ["text_delta", "done"]
    assert inner.closed
    assert slots._value == 1


async def test_error_event_releases_slot() -> None:
    error = ModelEvent(kind="error", data={"message": "failed", "retryable": False})
    slots = asyncio.Semaphore(1)
    assert await collect(LimitedProvider(FakeProvider([[error]]), slots), _request()) == [error]
    assert slots._value == 1


async def test_exception_closes_inner_and_releases_slot() -> None:
    inner = _TrackingProvider()
    inner.fail = True
    slots = asyncio.Semaphore(1)
    with pytest.raises(RuntimeError, match="request failed"):
        await collect(LimitedProvider(inner, slots), _request())
    assert inner.closed
    assert slots._value == 1


async def test_aclose_closes_inner_and_releases_slot_before_returning() -> None:
    inner = _TrackingProvider()
    slots = asyncio.Semaphore(1)
    stream = LimitedProvider(inner, slots).stream(_request(), CancelFlag())
    await anext(stream)
    assert slots._value == 0
    await stream.aclose()
    assert inner.closed
    assert slots._value == 1
    await stream.aclose()
    assert slots._value == 1


async def test_task_cancellation_closes_inner_and_releases_slot() -> None:
    inner = _TrackingProvider()
    inner.block = True
    slots = asyncio.Semaphore(1)
    task = asyncio.create_task(collect(LimitedProvider(inner, slots), _request()))
    try:
        await asyncio.wait_for(inner.waiting.wait(), timeout=1)
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert inner.closed
    assert slots._value == 1


async def test_cancellation_while_waiting_does_not_start_inner_or_release_others_slot() -> None:
    inner = FakeProvider([])
    slots = asyncio.Semaphore(1)
    await slots.acquire()
    task = asyncio.create_task(collect(LimitedProvider(inner, slots), _request()))
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert inner.requests == []
    assert slots._value == 0
    slots.release()
    assert slots._value == 1


async def test_cancel_token_midstream_is_forwarded() -> None:
    inner = _TrackingProvider()
    slots = asyncio.Semaphore(1)
    cancel = CancelFlag()
    stream = LimitedProvider(inner, slots).stream(_request(), cancel)
    await anext(stream)
    cancel.cancel()
    events = [event async for event in stream]
    assert len(events) == 1
    assert events[0].data == {"stop_reason": "cancelled"}
    assert inner.closed
    assert slots._value == 1


async def test_cleanup_exception_still_releases_slot() -> None:
    inner = _TrackingProvider()
    inner.close_fails = True
    slots = asyncio.Semaphore(1)
    stream = LimitedProvider(inner, slots).stream(_request(), CancelFlag())
    await anext(stream)
    with pytest.raises(RuntimeError, match="close failed"):
        await stream.aclose()
    assert inner.closed
    assert slots._value == 1


async def test_synchronous_stream_creation_exception_releases_slot() -> None:
    class FailingProvider(FakeProvider):
        def stream(self, req: ModelRequest, cancel: CancelToken) -> AsyncIterator[ModelEvent]:
            raise RuntimeError("cannot create stream")

    slots = asyncio.Semaphore(1)
    with pytest.raises(RuntimeError, match="cannot create stream"):
        await collect(LimitedProvider(FailingProvider([]), slots), _request())
    assert slots._value == 1


async def test_nonclosable_async_iterator_remains_supported() -> None:
    class Iterator:
        def __aiter__(self) -> AsyncIterator[ModelEvent]:
            return self

        async def __anext__(self) -> ModelEvent:
            raise StopAsyncIteration

    class IteratorProvider(FakeProvider):
        def stream(self, req: ModelRequest, cancel: CancelToken) -> AsyncIterator[ModelEvent]:
            return Iterator()

    slots = asyncio.Semaphore(1)
    assert await collect(LimitedProvider(IteratorProvider([]), slots), _request()) == []
    assert slots._value == 1
