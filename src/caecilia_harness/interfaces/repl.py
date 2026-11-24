"""The interactive `caecilia` session in line mode (CLI-02, ADR-0015).

Type a goal, look at the plan, approve it, watch the run, all in one terminal. Only reading
input and formatting output live here; the flow itself is `Session` (shared with `-p`, and with
the full-screen TUI and the GUI later).
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import tempfile
from collections.abc import Callable
from functools import partial
from pathlib import Path
from typing import Any

from caecilia_harness import __version__
from caecilia_harness.bootstrap import AppError
from caecilia_harness.core.graph import GraphError
from caecilia_harness.core.orchestrator import OrchestratorError
from caecilia_harness.interfaces.live import Echo, drive, on_sigint
from caecilia_harness.interfaces.prompter import LineReader
from caecilia_harness.interfaces.render import format_graph, format_plan, format_runs, format_status
from caecilia_harness.paths import harness_home
from caecilia_harness.planning.plan import Plan, PlanError
from caecilia_harness.planning.planner import PlannerError
from caecilia_harness.project_config import parse_check
from caecilia_harness.security import keys
from caecilia_harness.session import Session
from caecilia_harness.store.lock import WriterLockedError

PROMPT = "caecilia> "
Editor = Callable[[Path], None]

COMMANDS: list[tuple[str, str]] = [
    ("/help", "daftar command"),
    ("/plan <goal>", "hanya buat plan, tidak menjalankan"),
    ("/run [file]", "jalankan plan terakhir atau file plan"),
    ("/status", "run terakhir repo ini dan task-nya"),
    ("/runs", "daftar run repo ini"),
    ("/graph", "graf task run terakhir"),
    ("/resume [id]", "lanjutkan run yang belum selesai"),
    ("/save <file>", "simpan plan terakhir ke file"),
    ("/config", "opsi efektif dan asalnya (cli/config/default)"),
    ("/exit", "keluar (juga Ctrl+D; Ctrl+C saat idle membatalkan baris)"),
]

# Problems with the request: reported, then the session goes on.
_USER_ERRORS = (
    AppError,
    keys.MissingApiKeyError,
    WriterLockedError,
    OrchestratorError,
    GraphError,
    PlannerError,
    ValueError,  # MissingOptionError, ConfigError, unknown worker/provider
    OSError,  # a file given to /run or /save
)


def _cancel(task: asyncio.Future[Any]) -> None:
    task.cancel()


def default_editor(path: Path) -> None:
    """Open `path` in `$VISUAL` / `$EDITOR`, else notepad (Windows) or vi, and wait."""
    command = os.environ.get("VISUAL") or os.environ.get("EDITOR")
    argv = parse_check(command) if command else ["notepad" if os.name == "nt" else "vi"]
    subprocess.run([*argv, str(path)], check=False)


def history_path() -> Path:
    return harness_home() / "history"


class Repl:
    """Reads lines, runs slash commands, and turns plain text into a plan to approve."""

    def __init__(
        self,
        session: Session,
        reader: LineReader,
        echo: Echo,
        editor: Editor = default_editor,
        plain: bool = False,
        history: Path | None = None,
    ) -> None:
        self.session = session
        self.reader = reader
        self.echo = echo
        self.editor = editor
        self.plain = plain
        self.history = history

    # --- the loop ------------------------------------------------------------------------------

    async def run(self) -> None:
        """Until /exit or end of input."""
        for text in self.banner():
            self.echo(text)
        while True:
            raw = await self._read(PROMPT)
            if raw is None:
                self.echo("")
                return
            line = raw.strip()
            if not line:
                continue
            self._remember(line)
            # Ctrl+C while a command works (e.g. the planner) returns to the prompt. During a
            # run, `drive` takes Ctrl+C over (soft cancel, then stop) and restores this after.
            handling = asyncio.ensure_future(self.handle(line))
            try:
                with on_sigint(partial(_cancel, handling)):
                    keep_going = await handling
                if not keep_going:
                    return
            except asyncio.CancelledError:
                current = asyncio.current_task()
                if current is not None and current.cancelling():
                    raise
                self.echo("\n(dibatalkan)")
            except PlanError as exc:
                self._plan_problems(exc)
            except _USER_ERRORS as exc:
                self.echo(f"Error: {keys.redact(str(exc))}")

    async def _read(self, prompt: str) -> str | None:
        """One line; Ctrl+C while waiting drops the line ("") instead of ending the session."""
        reading = asyncio.ensure_future(self.reader.read(prompt))
        with on_sigint(partial(_cancel, reading)):
            try:
                return await reading
            except asyncio.CancelledError:
                current = asyncio.current_task()
                if current is not None and current.cancelling():
                    raise
                self.echo("\n(baris dibatalkan; /exit untuk keluar)")
                return ""

    def _remember(self, line: str) -> None:
        """Commands and goals go to the history file; answers to questions never do."""
        if self.history is None:
            return
        try:
            self.history.parent.mkdir(parents=True, exist_ok=True)
            with self.history.open("a", encoding="utf-8") as file:
                file.write(line.replace("\n", " ") + "\n")
        except OSError as exc:
            self.echo(f"(riwayat tidak tersimpan: {exc.strerror or exc})")
            self.history = None

    def banner(self) -> list[str]:
        s = self.session
        o = s.options
        if o.worker == "native":
            worker = f"native ({o.provider}, model {o.model or 'belum diisi: /config'})"
        else:
            worker = o.worker
        present = s.key_present()
        key = {
            None: "tidak dibutuhkan",
            True: f"{o.provider} ada",
            False: f"{o.provider} tidak ada (simpan dengan `caecilia key set {o.provider}`)",
        }[present]
        lines = [
            f"Caecilia Harness {__version__}",
            f"  repo    : {s.repo}",
            f"  worker  : {worker}",
            f"  API key : {key}",
        ]
        for run in s.unfinished_runs()[:3]:
            lines.append(f"  run belum selesai: {run.id} ({run.goal}) - ketik /resume {run.id}")
        lines.append(
            "Ketik goal untuk membuat plan, /help untuk daftar command, /exit untuk keluar."
        )
        return lines

    # --- commands ------------------------------------------------------------------------------

    async def handle(self, line: str) -> bool:
        """Act on one line; False ends the session."""
        if not line.startswith("/"):
            await self._goal(line)
            return True
        name, _, arg = line[1:].partition(" ")
        arg = arg.strip()
        match name.lower():
            case "exit" | "quit":
                return False
            case "help":
                width = max(len(command) for command, _ in COMMANDS)
                for command, meaning in COMMANDS:
                    self.echo(f"  {command:<{width}}  {meaning}")
                self.echo("  Teks tanpa / adalah goal: plan dibuat, lalu Anda ditanya.")
            case "plan":
                if not arg:
                    self.echo("Pakai: /plan <goal>")
                else:
                    self._show(await self.session.propose(arg))
                    self.echo("Jalankan dengan /run, atau simpan dengan /save <file>.")
            case "run":
                plan = self.session.load(Path(arg)) if arg else self.session.require_plan()
                if arg:
                    self._show(plan)
                await self._execute(plan)
            case "status":
                current = self.session.status(arg or None)
                self._lines(format_status(current, self.plain) if current else ["Belum ada run."])
            case "runs":
                found = self.session.runs()
                self._lines(format_runs(found, self.plain) if found else ["Belum ada run."])
            case "graph":
                self._lines(format_graph(self.session.graph(arg or None), self.plain))
            case "resume":
                await drive(self.session.resume(arg or None), self.plain, self.echo)
            case "save":
                if not arg:
                    self.echo("Pakai: /save <file>")
                else:
                    self.echo(f"Plan disimpan ke {self.session.save(Path(arg))}.")
            case "config":
                views = self.session.config_view()
                width = max(len(view.name) for view in views)
                for view in views:
                    self.echo(f"  {view.name:<{width}}  {view.value}  ({view.source})")
            case _:
                self.echo(f"Command tidak dikenal: /{name}. Ketik /help untuk daftar command.")
        return True

    async def _goal(self, goal: str) -> None:
        """Plain text: propose a plan, show it, then run / edit / keep it."""
        self.echo("Membuat plan...")
        plan = await self.session.propose(goal)
        self._show(plan)
        while True:
            answer = ((await self._read("Jalankan? [y]a / [e]dit / [n]o: ")) or "").strip().lower()
            if answer in ("y", "ya"):
                await self._execute(plan)
                return
            if answer in ("e", "edit"):
                edited = self._edit()
                if edited is None:
                    self.echo("Plan tidak dijalankan. Perbaiki dengan goal baru atau /run <file>.")
                    return
                plan = edited
                self._show(plan)
                continue
            if answer in ("n", "no", "tidak", ""):
                self.echo("Plan tidak dijalankan; masih tersimpan di sesi (/run, /save <file>).")
                return
            self.echo("Jawab y, e, atau n.")

    def _edit(self) -> Plan | None:
        """Write the plan to a temporary file, open the editor, validate what comes back."""
        text = self.session.plan_text()
        handle, name = tempfile.mkstemp(prefix="caecilia-plan-", suffix=".toml")
        os.close(handle)
        path = Path(name)
        try:
            path.write_text(text, encoding="utf-8", newline="\n")
            self.editor(path)
            edited = path.read_text(encoding="utf-8")
        finally:
            path.unlink(missing_ok=True)
        try:
            return self.session.replace_plan(edited, "plan yang diedit")
        except PlanError as exc:
            self._plan_problems(exc)
            return None

    async def _execute(self, plan: Plan) -> None:
        await drive(self.session.start(plan), self.plain, self.echo)

    # --- output --------------------------------------------------------------------------------

    def _show(self, plan: Plan) -> None:
        self._lines(format_plan(plan, self.plain))
        edges = sum(len(task.blocked_by) for task in plan.tasks)
        usage = self.session.last_plan_usage
        tokens = (
            f", token planner in {usage.input_tokens} / out {usage.output_tokens}"
            if usage.input_tokens or usage.output_tokens
            else ""
        )
        self.echo(f"Plan: {len(plan.tasks)} task, {edges} dependensi{tokens}.")

    def _plan_problems(self, exc: PlanError) -> None:
        self.echo(f"Plan tidak valid ({exc.source}):")
        for problem in exc.problems:
            self.echo(f"  - {problem}")

    def _lines(self, lines: list[str]) -> None:
        for line in lines:
            self.echo(line)
