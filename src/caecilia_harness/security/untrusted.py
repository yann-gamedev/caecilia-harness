"""Mark data that reaches a model as data, never instructions (zone Z3, AGENTS.md).

Used for tool output and documents in the native worker, and for the repo summary the planner
sends. One implementation, so every model sees the same markers.
"""

from __future__ import annotations

UNTRUSTED_OPEN = "<untrusted-data"
UNTRUSTED_CLOSE = "</untrusted-data>"
_NEUTRALISED_CLOSE = "</untrusted-data_>"


def wrap_untrusted(content: str, source: str, ok: bool | None = None) -> str:
    """Wrap data from files, commands or documents so the model treats it as data only.

    The closing marker is neutralised inside `content`, so the data cannot end the block early
    and smuggle text that looks like it came from the harness.
    """
    safe = content.replace(UNTRUSTED_CLOSE, _NEUTRALISED_CLOSE)
    status = "" if ok is None else f' ok="{str(ok).lower()}"'
    return f'{UNTRUSTED_OPEN} source="{source}"{status}>\n{safe}\n{UNTRUSTED_CLOSE}'
