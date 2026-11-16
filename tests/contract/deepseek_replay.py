"""Replay recorded DeepSeek responses through `httpx.MockTransport` (no network)."""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "deepseek"


def load(name: str) -> dict[str, Any]:
    data: dict[str, Any] = json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))
    return data


def sse(chunks: list[dict[str, Any]], done: bool = True, keep_alive: bool = False) -> bytes:
    """Render chunks as a server-sent event stream."""
    lines = [": keep-alive\n\n"] if keep_alive else []
    lines += [f"data: {json.dumps(chunk)}\n\n" for chunk in chunks]
    if done:
        lines.append("data: [DONE]\n\n")
    return "".join(lines).encode("utf-8")


def response_for(recording: dict[str, Any], echo_key: str | None = None) -> httpx.Response:
    if recording["status"] == 200:
        return httpx.Response(
            200, headers={"content-type": "text/event-stream"}, content=sse(recording["chunks"])
        )
    body = json.loads(json.dumps(recording["body"]))
    if echo_key is not None:
        # Worst case: the server repeats the key it was sent.
        body["error"]["message"] += f": {echo_key}"
    return httpx.Response(recording["status"], json=body)


class Replay:
    """Transport answering each request with the next recording (the last one repeats)."""

    def __init__(self, *names: str, echo_key: bool = False) -> None:
        self.recordings = [load(name) for name in names]
        self.echo_key = echo_key
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        index = min(len(self.requests), len(self.recordings) - 1)
        self.requests.append(request)
        key = request.headers.get("authorization", "").removeprefix("Bearer ")
        return response_for(self.recordings[index], key if self.echo_key else None)

    def bodies(self) -> list[dict[str, Any]]:
        return [json.loads(request.content) for request in self.requests]


def client(handler: Callable[[httpx.Request], httpx.Response]) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))
