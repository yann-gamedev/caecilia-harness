"""DeepSeek chat-completions adapter (`core.ports.ModelProvider`).

Authentication is an API key only, sent as `Authorization: Bearer <key>` to the official API
(ADR-0007): no consumer login, no session tokens, no other client's identity. The harness
identifies itself with its own User-Agent.

Terms that apply to this API: DeepSeek Open Platform Terms of Service,
https://cdn.deepseek.com/policies/en-US/deepseek-open-platform-terms-of-service.html
API reference: https://api-docs.deepseek.com/api/create-chat-completion

Implemented from the documented wire format with plain `httpx`; no vendor SDK is involved.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any

import httpx

from caecilia_harness import __version__
from caecilia_harness.core.models import Message, ModelCaps, ModelEvent, ModelRequest
from caecilia_harness.core.ports import CancelToken
from caecilia_harness.security.keys import redact

DEFAULT_BASE_URL = "https://api.deepseek.com"
DEFAULT_CAPS = ModelCaps(
    supports_tools=True,
    supports_streaming=True,
    reports_usage=True,
    # Conservative defaults; the limits differ per model and are not hard-coded by name here.
    context_window=64_000,
    max_output_tokens=8_192,
)
_TIMEOUT = httpx.Timeout(connect=15.0, read=300.0, write=30.0, pool=15.0)
_STOP_REASONS = {"stop": "end_turn", "tool_calls": "tool_use", "length": "max_tokens"}
_ERROR_BODY_CHARS = 300


def _done(stop_reason: str) -> ModelEvent:
    return ModelEvent(kind="done", data={"stop_reason": stop_reason})


def _wire_message(message: Message) -> dict[str, Any]:
    if message.role == "tool":
        return {"role": "tool", "tool_call_id": message.tool_call_id, "content": message.content}
    if message.role == "assistant" and message.tool_calls:
        return {
            "role": "assistant",
            "content": message.content or None,
            "tool_calls": [
                {
                    "id": call.id,
                    "type": "function",
                    "function": {"name": call.name, "arguments": json.dumps(call.input)},
                }
                for call in message.tool_calls
            ],
        }
    return {"role": message.role, "content": message.content}


def _wire_body(req: ModelRequest) -> dict[str, Any]:
    body: dict[str, Any] = {
        "model": req.model,
        "messages": [_wire_message(message) for message in req.messages],
        "stream": True,
        "stream_options": {"include_usage": True},
    }
    if req.tools:
        body["tools"] = [
            {
                "type": "function",
                "function": {
                    "name": tool.name,
                    "description": tool.description,
                    "parameters": tool.input_schema,
                },
            }
            for tool in req.tools
        ]
    if req.max_output_tokens is not None:
        body["max_tokens"] = req.max_output_tokens
    return body


def _parse_arguments(raw: str) -> dict[str, Any]:
    try:
        parsed = json.loads(raw) if raw.strip() else {}
    except json.JSONDecodeError:
        parsed = None
    if isinstance(parsed, dict):
        return parsed
    # Hand the raw text back as an invalid input: tool validation rejects it and the model
    # gets a clear error to correct, instead of the call being silently altered.
    return {"_invalid_arguments": raw}


class DeepSeekProvider:
    id: str = "deepseek"

    def __init__(
        self,
        api_key: str,
        client: httpx.AsyncClient | None = None,
        base_url: str = DEFAULT_BASE_URL,
        caps: ModelCaps | None = None,
    ) -> None:
        self._api_key = api_key
        self._client = client  # injected in tests (MockTransport); otherwise one per stream
        self._url = base_url.rstrip("/") + "/chat/completions"
        self._caps = caps or DEFAULT_CAPS

    def capabilities(self, model: str) -> ModelCaps:
        return self._caps

    def _safe(self, text: str) -> str:
        """No message leaving this adapter may contain the key."""
        return redact(text).replace(self._api_key, "***")

    def _error(self, message: str, retryable: bool) -> ModelEvent:
        return ModelEvent(
            kind="error", data={"message": self._safe(message), "retryable": retryable}
        )

    async def stream(self, req: ModelRequest, cancel: CancelToken) -> AsyncIterator[ModelEvent]:
        """Yield events for one request; always ends with exactly one `done` or `error`."""
        if cancel.cancelled:
            yield _done("cancelled")
            return
        client = self._client or httpx.AsyncClient(timeout=_TIMEOUT)
        try:
            async for event in self._events(client, req, cancel):
                yield event
        except (httpx.HTTPError, json.JSONDecodeError) as exc:
            # Network failures, timeouts and broken streams may succeed on a retry.
            yield self._error(f"{type(exc).__name__}: {exc}", retryable=True)
        finally:
            if self._client is None:
                await client.aclose()

    async def _events(
        self, client: httpx.AsyncClient, req: ModelRequest, cancel: CancelToken
    ) -> AsyncIterator[ModelEvent]:
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "User-Agent": f"caecilia-harness/{__version__}",
            "Accept": "text/event-stream",
        }
        async with client.stream("POST", self._url, headers=headers, json=_wire_body(req)) as resp:
            if resp.status_code >= 400:
                yield self._http_error(resp.status_code, await resp.aread())
                return

            calls: dict[int, dict[str, str]] = {}
            usage: dict[str, Any] | None = None
            finish_reason: str | None = None
            async for line in resp.aiter_lines():
                if cancel.cancelled:
                    yield _done("cancelled")
                    return
                if not line.startswith("data:"):
                    continue  # blank separators and ": keep-alive" comments
                data = line.removeprefix("data:").strip()
                if data == "[DONE]":
                    break
                chunk = json.loads(data)
                usage = chunk.get("usage") or usage
                for choice in chunk.get("choices") or []:
                    delta = choice.get("delta") or {}
                    # `reasoning_content` (thinking mode) is not part of the ModelEvent
                    # contract and must not be sent back to the API, so it is dropped.
                    if delta.get("content"):
                        yield ModelEvent(kind="text_delta", data={"text": delta["content"]})
                    for fragment in delta.get("tool_calls") or []:
                        call = calls.setdefault(
                            int(fragment.get("index", 0)), {"id": "", "name": "", "arguments": ""}
                        )
                        function = fragment.get("function") or {}
                        call["id"] = fragment.get("id") or call["id"]
                        call["name"] = function.get("name") or call["name"]
                        call["arguments"] += function.get("arguments") or ""
                    finish_reason = choice.get("finish_reason") or finish_reason

        if finish_reason is None:
            yield self._error("stream ended before the response was complete", retryable=True)
            return
        for index in sorted(calls):
            call = calls[index]
            yield ModelEvent(
                kind="tool_call",
                data={
                    "id": call["id"],
                    "name": call["name"],
                    "input": _parse_arguments(call["arguments"]),
                },
            )
        if usage:
            yield ModelEvent(
                kind="usage",
                data={
                    "input_tokens": int(usage.get("prompt_tokens", 0)),
                    "output_tokens": int(usage.get("completion_tokens", 0)),
                },
            )
        yield _done("tool_use" if calls else _STOP_REASONS.get(finish_reason, "end_turn"))

    def _http_error(self, status: int, body: bytes) -> ModelEvent:
        text = body.decode("utf-8", errors="replace")
        detail = text.strip()[:_ERROR_BODY_CHARS]
        try:
            parsed = json.loads(text)
            detail = str(parsed["error"]["message"])
        except (json.JSONDecodeError, KeyError, TypeError):
            pass
        # 429 rate limit and 5xx server/overload errors are worth retrying; 400/401/402/403/422
        # are caused by the request or the account and will not fix themselves.
        return self._error(f"HTTP {status}: {detail}", retryable=status == 429 or status >= 500)
