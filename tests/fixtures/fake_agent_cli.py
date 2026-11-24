"""A tiny stand-in for an external agent CLI, used by the EXT-01 tests and demos.

It speaks the generic line-delimited JSON protocol `workers.external.base.parse_line` understands:

    {"event": "start", ...}
    {"event": "text", "text": "..."}
    {"event": "tool", "name": "write_file", "input": {...}}
    {"event": "usage", "input_tokens": 10, "output_tokens": 5}
    {"event": "result", "ok": true, "summary": "..."}

Behaviour is chosen with environment variables, so a test never needs a real agent:

- `FAKE_AGENT_MODE`: `ok` (default), `fail`, `crash`, `hang`, `no-result`.
- `FAKE_AGENT_FILE`: file written in the working directory (default `FAKE_AGENT.md`).
- `FAKE_AGENT_ECHO`: text echoed in a `text` event (used to check log redaction).
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

VERSION = "0.1.0"


def emit(payload: dict[str, object]) -> None:
    sys.stdout.write(json.dumps(payload) + "\n")
    sys.stdout.flush()


def write_task_file(task: str) -> str:
    name = os.environ.get("FAKE_AGENT_FILE", "FAKE_AGENT.md")
    target = Path.cwd() / name
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(f"# fake agent\n\n{task}\n", encoding="utf-8", newline="\n")
    return name


def main(argv: list[str]) -> int:
    if "--version" in argv:
        print(f"fake-agent {VERSION}")
        return 0
    task = sys.stdin.read()
    mode = os.environ.get("FAKE_AGENT_MODE", "ok")
    echo = os.environ.get("FAKE_AGENT_ECHO")
    emit({"event": "start", "mode": mode})
    if echo:
        emit({"event": "text", "text": echo})
    if mode == "hang":
        while True:
            time.sleep(0.2)
    if mode == "crash":
        emit({"event": "text", "text": "about to crash"})
        raise RuntimeError("fake agent crashed on purpose")
    if mode == "no-result":
        emit({"event": "text", "text": "finished without a result line"})
        return 0
    name = write_task_file(task)
    emit({"event": "tool", "name": "write_file", "input": {"path": name}})
    emit({"event": "usage", "input_tokens": 10, "output_tokens": 5})
    if mode == "fail":
        emit({"event": "result", "ok": False, "error": "fake_agent_failed"})
        return 0
    emit({"event": "result", "ok": True, "summary": f"wrote {name}"})
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
