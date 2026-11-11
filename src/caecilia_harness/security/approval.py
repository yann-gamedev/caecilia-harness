"""Approval policy for commands a worker wants to execute (ADR-0008: default `ask`)."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict

from caecilia_harness.core.models import Decision


class ExecRequest(BaseModel):
    """A command a task wants to run, as shown to the person approving it."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    argv: list[str]
    cwd: Path
    task_id: str


#: Asks a person (CLI prompt, later TUI/GUI) whether `ExecRequest` may run.
Approver = Callable[[ExecRequest], Awaitable[bool]]


@dataclass
class ApprovalPolicy:
    """Decide `allow` / `ask` / `deny` for an exec request.

    `remembered` holds argv prefixes a person approved "always" during this run, e.g.
    `("uv", "run", "pytest")`; matching requests are allowed without asking again.
    """

    mode: Literal["allow", "ask", "deny"] = "ask"
    remembered: set[tuple[str, ...]] = field(default_factory=set)

    def remembers(self, argv: Sequence[str]) -> bool:
        """True if a remembered prefix covers `argv`."""
        return any(prefix and tuple(argv[: len(prefix)]) == prefix for prefix in self.remembered)

    def decide(self, req: ExecRequest) -> Decision:
        if self.mode == "deny":
            return "deny"
        if self.remembers(req.argv):
            return "allow"
        return self.mode
