"""Workers that drive an external agent CLI (Claude Code, Codex, Gemini CLI, fake agent).

Each adapter implements `core.ports.WorkerAdapter` on top of `AgentProcess`; the profile says
which program and version, the parser turns its output into `WorkerEvent`s. The harness computes
the diff itself, so an agent's own file report is never trusted (ADR-0018).
"""

from caecilia_harness.workers.external.base import ExternalAgentWorker, ParsedLine, parse_line
from caecilia_harness.workers.external.process import AgentProcess, AgentProcessError
from caecilia_harness.workers.external.profile import (
    AGENT_PROFILES,
    AgentDetection,
    AgentProfile,
    AgentProfileError,
    detect_agent,
    profile_for,
)

__all__ = [
    "AGENT_PROFILES",
    "AgentDetection",
    "AgentProcess",
    "AgentProcessError",
    "AgentProfile",
    "AgentProfileError",
    "ExternalAgentWorker",
    "ParsedLine",
    "detect_agent",
    "parse_line",
    "profile_for",
]
