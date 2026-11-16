"""DeepSeek adapter against recorded streams: provider contract and mapping (backlog M1-14)."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest

from caecilia_harness import __version__
from caecilia_harness.core.cancel import CancelFlag
from caecilia_harness.core.models import Message, ModelRequest, ToolCall, ToolSpec
from caecilia_harness.providers.deepseek import DeepSeekProvider
from caecilia_harness.security import keys
from tests.contract.deepseek_replay import Replay, client, load, sse
from tests.contract.model_provider_contract import (
    ModelProviderContract,
    assert_single_terminal_at_end,
    collect,
)

CANARY = "sk-test-CANARY"
TOOL = ToolSpec(
    name="write_file",
    description="Write a file.",
    input_schema={"type": "object", "properties": {"path": {"type": "string"}}},
)


def _request(*messages: Message, tools: list[ToolSpec] | None = None) -> ModelRequest:
    msgs = list(messages) or [Message(role="user", content="hi")]
    return ModelRequest(model="some-model", messages=msgs, tools=tools or [])


def _provider(handler: Any) -> DeepSeekProvider:
    return DeepSeekProvider(CANARY, client=client(handler))


@pytest.fixture(autouse=True)
def _key_is_known(monkeypatch: pytest.MonkeyPatch) -> None:
    # The key reaches the provider through get_api_key in real use, which registers it.
    monkeypatch.setenv("DEEPSEEK_API_KEY", CANARY)
    assert keys.get_api_key("deepseek") == CANARY


class TestDeepSeekContract(ModelProviderContract):
    @pytest.fixture
    async def provider(self) -> AsyncIterator[DeepSeekProvider]:
        http = client(Replay("text"))
        yield DeepSeekProvider(CANARY, client=http)
        await http.aclose()

    @pytest.fixture
    def simple_request(self) -> ModelRequest:
        return _request()


async def test_text_stream() -> None:
    events = await collect(_provider(Replay("text")), _request())
    assert [e.kind for e in events] == ["text_delta", "text_delta", "usage", "done"]
    assert "".join(e.data["text"] for e in events[:2]) == "Selesai: NOTES.md ditulis."
    assert events[2].data == {"input_tokens": 120, "output_tokens": 9}
    assert events[3].data == {"stop_reason": "end_turn"}


async def test_tool_call_stream() -> None:
    events = await collect(_provider(Replay("tool_call")), _request(tools=[TOOL]))
    assert [e.kind for e in events] == ["text_delta", "tool_call", "usage", "done"]
    assert events[0].data == {"text": "Saya tulis filenya."}  # reasoning_content is dropped
    assert events[1].data == {
        "id": "call_0_rec",
        "name": "write_file",
        "input": {"path": "NOTES.md", "content": "catatan\n"},
    }
    assert events[3].data == {"stop_reason": "tool_use"}


async def test_request_mapping() -> None:
    replay = Replay("text")
    history = [
        Message(role="system", content="be brief"),
        Message(role="user", content="write notes"),
        Message(
            role="assistant",
            content="",
            tool_calls=[ToolCall(id="c1", name="write_file", input={"path": "a.md"})],
        ),
        Message(role="tool", content="wrote a.md", tool_call_id="c1"),
    ]
    request = ModelRequest(
        model="some-model", messages=history, tools=[TOOL], max_output_tokens=512
    )
    await collect(_provider(replay), request)

    [sent] = replay.requests
    assert str(sent.url) == "https://api.deepseek.com/chat/completions"
    assert sent.headers["authorization"] == f"Bearer {CANARY}"
    # ADR-0007: the harness identifies as itself and sends no other client's fingerprint.
    assert sent.headers["user-agent"] == f"caecilia-harness/{__version__}"
    assert not [h for h in sent.headers if h.lower().startswith("x-stainless")]

    [body] = replay.bodies()
    assert body["model"] == "some-model"
    assert body["stream"] is True and body["stream_options"] == {"include_usage": True}
    assert body["max_tokens"] == 512
    assert body["messages"] == [
        {"role": "system", "content": "be brief"},
        {"role": "user", "content": "write notes"},
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": "c1",
                    "type": "function",
                    "function": {"name": "write_file", "arguments": '{"path": "a.md"}'},
                }
            ],
        },
        {"role": "tool", "tool_call_id": "c1", "content": "wrote a.md"},
    ]
    assert body["tools"] == [
        {
            "type": "function",
            "function": {
                "name": "write_file",
                "description": "Write a file.",
                "parameters": TOOL.input_schema,
            },
        }
    ]


async def test_request_without_tools_or_limit_omits_them() -> None:
    replay = Replay("text")
    await collect(_provider(replay), _request())
    [body] = replay.bodies()
    assert "tools" not in body and "max_tokens" not in body


@pytest.mark.parametrize(
    ("status", "retryable"),
    [(429, True), (500, True), (503, True), (400, False), (401, False), (402, False), (403, False)],
)
async def test_http_errors_map_to_retryable(status: int, retryable: bool) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, json={"error": {"message": "nope"}})

    events = await collect(_provider(handler), _request())
    assert [e.kind for e in events] == ["error"]
    assert events[0].data["retryable"] is retryable
    assert str(status) in events[0].data["message"]


async def test_recorded_429_is_retryable() -> None:
    events = await collect(_provider(Replay("error_429")), _request())
    assert events[0].data == {
        "message": "HTTP 429: Rate limit reached for requests",
        "retryable": True,
    }


async def test_401_message_never_contains_the_key() -> None:
    events = await collect(_provider(Replay("error_401", echo_key=True)), _request())
    assert [e.kind for e in events] == ["error"]
    assert events[0].data["retryable"] is False
    assert CANARY not in events[0].data["message"]
    assert "***" in events[0].data["message"]


async def test_non_json_error_body() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(502, text=f"<html>bad gateway {CANARY}</html>")

    [event] = await collect(_provider(handler), _request())
    assert event.data["retryable"] is True
    assert CANARY not in event.data["message"]


async def test_transport_failure_is_retryable() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError(f"connection refused (key {CANARY})", request=request)

    [event] = await collect(_provider(handler), _request())
    assert event.kind == "error" and event.data["retryable"] is True
    assert CANARY not in event.data["message"]


async def test_stream_cut_off_is_a_retryable_error() -> None:
    chunks = load("text")["chunks"][:2]

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=sse(chunks, done=False))

    events = await collect(_provider(handler), _request())
    assert_single_terminal_at_end(events)
    assert events[-1].kind == "error" and events[-1].data["retryable"] is True


async def test_keep_alive_comments_and_blank_lines_are_ignored() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=sse(load("text")["chunks"], keep_alive=True))

    events = await collect(_provider(handler), _request())
    assert events[-1].data == {"stop_reason": "end_turn"}


async def test_malformed_tool_arguments_are_passed_on_for_the_model_to_fix() -> None:
    chunks = load("tool_call")["chunks"]
    chunks[4]["choices"][0]["delta"]["tool_calls"][0]["function"]["arguments"] = "tent: oops"

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=sse(chunks))

    events = await collect(_provider(handler), _request(tools=[TOOL]))
    [call] = [e for e in events if e.kind == "tool_call"]
    assert call.data["name"] == "write_file"
    assert list(call.data["input"]) == ["_invalid_arguments"]


async def test_finish_reason_length_maps_to_max_tokens() -> None:
    chunks = load("text")["chunks"]
    chunks[-1]["choices"][0]["finish_reason"] = "length"

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=sse(chunks))

    events = await collect(_provider(handler), _request())
    assert events[-1].data == {"stop_reason": "max_tokens"}


async def test_cancel_before_start_sends_no_request() -> None:
    replay = Replay("text")
    cancel = CancelFlag()
    cancel.cancel()
    events = await collect(_provider(replay), _request(), cancel)
    assert [e.data for e in events] == [{"stop_reason": "cancelled"}]
    assert replay.requests == []


async def test_cancel_mid_stream() -> None:
    provider = _provider(Replay("text"))
    cancel = CancelFlag()
    received = []
    async for event in provider.stream(_request(), cancel):
        received.append(event)
        if event.kind == "text_delta":
            cancel.cancel()
    assert [e.kind for e in received] == ["text_delta", "done"]
    assert received[-1].data == {"stop_reason": "cancelled"}


def test_capabilities_report_usage_and_tools() -> None:
    caps = DeepSeekProvider(CANARY).capabilities("some-model")
    assert caps.supports_tools and caps.supports_streaming and caps.reports_usage
