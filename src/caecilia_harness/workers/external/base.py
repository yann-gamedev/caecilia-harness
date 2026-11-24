"""`WorkerAdapter` for an external agent CLI (EXT-01, ADR-0018).

The harness starts the agent's own program in the task worktree and reads its line-delimited
output. It never trusts the agent's report about files: the diff is always computed by the
orchestrator from the worktree (`capture_diff`), exactly as for the native worker. Raw output is
kept as a redacted artifact, so a key the agent echoes never reaches a log or an event.
"""

from __future__ import annotations

import asyncio
import json
import re
from collections.abc import AsyncIterator, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from caecilia_harness import artifacts
from caecilia_harness.core.models import (
    Budget,
    ContextPack,
    RunHandle,
    TaskSpec,
    WorkerCapabilities,
    WorkerEvent,
    WorkerResult,
    Workspace,
)
from caecilia_harness.security import keys
from caecilia_harness.workers.errors import UnknownRunHandleError
from caecilia_harness.workers.external.process import AgentProcess, AgentProcessError
from caecilia_harness.workers.external.profile import AgentProfile
from caecilia_harness.workers.native.prompts import build_user_message

#: How much of the agent's stderr ends up in the failure reason.
_ERROR_TAIL = 600


@dataclass(frozen=True)
class _Report:
    """The agent's own final report (never trusted for files, only for its verdict)."""

    ok: bool
    summary: str = ""
    error: str | None = None


@dataclass(frozen=True)
class ParsedLine:
    """What one output line means: a progress event, a final report, or both."""

    event: WorkerEvent | None = None
    report: _Report | None = None


Parser = Callable[[str], ParsedLine | None]


def parse_line(line: str) -> ParsedLine | None:
    """The generic agent protocol used by the fake agent and by adapters that need no quirks.

    One JSON object per line: `{"event": "text", "text": ...}`, `{"event": "tool", ...}`,
    `{"event": "usage", ...}`, `{"event": "error", "message": ...}`, and a final
    `{"event": "result", "ok": true, "summary": ...}`. Anything else is kept as a log line.
    """
    text = line.strip()
    if not text:
        return None
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return ParsedLine(event=WorkerEvent(kind="log", data={"text": text}))
    if not isinstance(data, dict):
        return ParsedLine(event=WorkerEvent(kind="log", data={"unparsed": text}))
    kind = str(data.get("event") or data.get("type") or "")
    if kind == "result":
        if not isinstance(data.get("ok"), bool):
            raise ValueError("agent result must contain boolean ok")
        ok = data["ok"]
        error = data.get("error")
        return ParsedLine(
            report=_Report(
                ok=ok,
                summary=str(data.get("summary") or ""),
                error=str(error) if error else (None if ok else "agent_reported_failure"),
            )
        )
    if kind in ("text", "log"):
        return ParsedLine(event=WorkerEvent(kind="log", data={"text": str(data.get("text") or "")}))
    if kind == "tool":
        return ParsedLine(
            event=WorkerEvent(
                kind="tool_call",
                data={"name": str(data.get("name") or ""), "input": data.get("input") or {}},
            )
        )
    if kind == "tool_result":
        return ParsedLine(
            event=WorkerEvent(
                kind="tool_result",
                data={"name": str(data.get("name") or ""), "ok": bool(data.get("ok", True))},
            )
        )
    if kind == "usage":
        usage = {name: data.get(name, 0) for name in ("input_tokens", "output_tokens")}
        if any(type(value) is not int or value < 0 for value in usage.values()):
            raise ValueError("agent usage must contain non-negative integer tokens")
        return ParsedLine(event=WorkerEvent(kind="usage", data=usage, durable=True))
    if kind == "error":
        return ParsedLine(
            event=WorkerEvent(kind="log", data={"error": str(data.get("message") or "agent error")})
        )
    if kind in ("start", "progress"):
        payload = {k: v for k, v in data.items() if k not in ("event", "type")}
        return ParsedLine(event=WorkerEvent(kind="progress", data=payload))
    return ParsedLine(event=WorkerEvent(kind="log", data={"unparsed": text}))


@dataclass
class _Run:
    spec: TaskSpec
    proc: AgentProcess | None = None
    queue: asyncio.Queue[WorkerEvent | None] = field(default_factory=asyncio.Queue)
    task: asyncio.Task[WorkerResult] | None = None
    report: _Report | None = None
    lines: list[str] = field(default_factory=list)
    cancelled: bool = False
    log_ref: str | None = None
    log_text: str = ""


class ExternalAgentWorker:
    """`core.ports.WorkerAdapter` that runs an agent CLI headless in the task worktree."""

    def __init__(
        self,
        profile: AgentProfile,
        *,
        env: Mapping[str, str],
        parse: Parser | None = None,
        grace_s: float = 5.0,
        secrets: Sequence[str] = (),
    ) -> None:
        self.profile = profile
        self.id: str = profile.name
        self.env = dict(env)
        self.parse = parse or parse_line
        self.grace_s = grace_s
        #: Values the worker itself injects (an API key), always scrubbed from output.
        self._secrets = tuple(secret for secret in secrets if secret)
        self._runs: dict[tuple[str, int], _Run] = {}
        self._attempts: dict[str, int] = {}

    def capabilities(self) -> WorkerCapabilities:
        return WorkerCapabilities(
            streams_events=True,
            supports_cancel=True,
            supports_resume=False,
            reports_usage=False,
            edits_files=True,
            needs_network=True,
        )

    def argv(self) -> list[str]:
        """The program and arguments started for every task of this profile."""
        argv = [self.profile.binary, *self.profile.extra_args]
        if self.profile.model:
            argv += ["--model", self.profile.model]
        return argv

    async def start(
        self, spec: TaskSpec, ctx: ContextPack, ws: Workspace, budget: Budget
    ) -> RunHandle:
        attempt = self._attempts.get(spec.id, 0) + 1
        self._attempts[spec.id] = attempt
        handle = RunHandle(task_id=spec.id, worker=self.id, attempt=attempt)
        run = _Run(spec=spec)
        self._runs[(handle.task_id, handle.attempt)] = run
        proc = AgentProcess(self.argv(), Path(ws.path), self.env, grace_s=self.grace_s)
        try:
            await proc.start()
        except (OSError, AgentProcessError) as exc:
            error = self._redact(f"agent_spawn_failed:{exc}")
            self._failed(run, error)
            return handle
        run.proc = proc
        handle = handle.model_copy(update={"pid": proc.pid})
        run.task = asyncio.create_task(self._supervise(run, build_user_message(spec, ctx)))
        return handle

    def events(self, handle: RunHandle) -> AsyncIterator[WorkerEvent]:
        return self._events(self._get(handle))

    async def _events(self, run: _Run) -> AsyncIterator[WorkerEvent]:
        while True:
            event = await run.queue.get()
            if event is None:
                return
            yield event

    async def cancel(self, handle: RunHandle, mode: Literal["soft", "hard"]) -> None:
        run = self._get(handle)
        run.cancelled = True
        if run.proc is not None:
            await run.proc.cancel(mode)

    async def result(self, handle: RunHandle) -> WorkerResult:
        run = self._get(handle)
        assert run.task is not None  # noqa: S101 - set in start() (or by _failed)
        return await asyncio.shield(run.task)

    def log_digest(self, handle: RunHandle) -> str | None:
        """Artifact hash of the redacted raw output, once the run has finished."""
        return self._get(handle).log_ref

    def log_text(self, handle: RunHandle) -> str:
        """The redacted raw output kept in memory (tests and `caecilia logs` later)."""
        return self._get(handle).log_text

    def _get(self, handle: RunHandle) -> _Run:
        try:
            return self._runs[(handle.task_id, handle.attempt)]
        except KeyError:
            raise UnknownRunHandleError(handle) from None

    def _failed(self, run: _Run, error: str) -> None:
        run.task = asyncio.create_task(
            self._finish(
                run, WorkerResult(task_id=run.spec.id, success=False, summary="", error=error)
            )
        )

    async def _supervise(self, run: _Run, prompt: str) -> WorkerResult:
        assert run.proc is not None  # noqa: S101 - start() sets it before supervising
        sender = asyncio.create_task(run.proc.send(prompt))
        error: str | None = None
        try:
            async for line in run.proc.lines():
                run.lines.append(self._redact(line))
                parsed = self.parse(line)
                if parsed is None:
                    continue
                if parsed.report is not None:
                    run.report = parsed.report
                if parsed.event is not None:
                    await run.queue.put(self._redacted_event(parsed.event))
            code = await run.proc.wait()
            await sender
            error = None if code == 0 else f"agent_exited:{code}"
        except asyncio.CancelledError:
            await run.proc.cancel("hard")
            raise
        except Exception as exc:  # report, never crash the orchestrator
            error = f"worker_crashed:{exc!r}"
            await run.proc.cancel("hard")
        finally:
            if not sender.done():
                sender.cancel()
            await asyncio.gather(sender, return_exceptions=True)
            try:
                await run.proc.close()
            except Exception as exc:
                error = f"agent_cleanup_failed:{exc!r}"
        return await self._finish(run, self._result_from(run, error))

    def _result_from(self, run: _Run, error: str | None) -> WorkerResult:
        if run.cancelled:
            return WorkerResult(task_id=run.spec.id, success=False, summary="", error="cancelled")
        if error is not None:
            return WorkerResult(
                task_id=run.spec.id, success=False, summary="", error=self._with_stderr(run, error)
            )
        report = run.report
        if report is None:
            return WorkerResult(
                task_id=run.spec.id,
                success=False,
                summary="",
                error=self._with_stderr(run, "agent_no_result"),
            )
        return WorkerResult(
            task_id=run.spec.id,
            success=report.ok,
            summary=report.summary,
            error=report.error or (None if report.ok else "agent_reported_failure"),
        )

    def _with_stderr(self, run: _Run, error: str) -> str:
        assert run.proc is not None  # noqa: S101
        tail = self._redact(run.proc.stderr_text).strip()[-_ERROR_TAIL:]
        return f"{error}: {tail}" if tail else error

    async def _finish(self, run: _Run, result: WorkerResult) -> WorkerResult:
        assert run.proc is not None or result.error is not None  # noqa: S101
        result = result.model_copy(
            update={
                "summary": self._redact(result.summary),
                "error": self._redact(result.error) if result.error is not None else None,
            }
        )
        stderr = self._redact(run.proc.stderr_text) if run.proc is not None else ""
        raw = "\n".join(run.lines)
        if stderr.strip():
            raw = f"{raw}\n-- stderr --\n{stderr}" if raw else stderr
        run.log_text = self._redact(raw)
        if run.log_text:
            try:
                run.log_ref = artifacts.put_bytes(run.log_text.encode("utf-8"), "log")
            except OSError as exc:
                result = WorkerResult(
                    task_id=run.spec.id,
                    success=False,
                    summary=result.summary,
                    error=self._redact(f"worker_log_failed:{exc}"),
                )
        await run.queue.put(
            WorkerEvent(
                kind="progress",
                data={
                    "status": "finished",
                    "success": result.success,
                    "summary": result.summary,
                    "error": result.error,
                    "log": run.log_ref,
                },
                durable=True,
            )
        )
        await run.queue.put(None)
        return result

    def _redact(self, text: str) -> str:
        for secret in self._secrets:
            text = text.replace(secret, "***")
        text = keys.redact(text)
        return re.sub(r"\b(?:sk-[A-Za-z0-9_-]{8,}|AIza[A-Za-z0-9_-]{20,})\b", "***", text)

    def _redacted_event(self, event: WorkerEvent) -> WorkerEvent:
        return event.model_copy(update={"data": self._redact_value(event.data)})

    def _redact_value(self, value: object) -> Any:
        if isinstance(value, str):
            return self._redact(value)
        if isinstance(value, dict):
            return {
                self._redact(key) if isinstance(key, str) else key: self._redact_value(item)
                for key, item in value.items()
            }
        if isinstance(value, list):
            return [self._redact_value(item) for item in value]
        return value


__all__ = ["ExternalAgentWorker", "ParsedLine", "Parser", "parse_line"]
