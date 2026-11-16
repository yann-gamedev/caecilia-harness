"""`UserPrompter` for the terminal running `caecilia run` (M1: approval happens there)."""

from __future__ import annotations

import asyncio
import sys
import threading
from collections.abc import Callable

from caecilia_harness.core.models import Action, GateReport
from caecilia_harness.security.approval import ApprovalPolicy

ReadLine = Callable[[str], str]
Echo = Callable[[str], None]

_TAIL_LINES = 15


def _stdin_is_tty() -> bool:
    return sys.stdin is not None and sys.stdin.isatty()


def _echo(text: str) -> None:
    print(text, flush=True)


class TerminalPrompter:
    """Asks y/n questions on the terminal.

    Without a TTY nobody can answer, so every question is refused (never auto-approved).
    Answering `a` remembers the command as a prefix for the rest of the run.
    """

    def __init__(
        self,
        policy: ApprovalPolicy | None = None,
        read_line: ReadLine = input,
        isatty: Callable[[], bool] = _stdin_is_tty,
        echo: Echo = _echo,
    ) -> None:
        self.policy = policy
        self.read_line = read_line
        self.isatty = isatty
        self.echo = echo
        self.remembered: set[tuple[str, ...]] = set()

    def _remembers(self, argv: list[str]) -> bool:
        return any(prefix and tuple(argv[: len(prefix)]) == prefix for prefix in self.remembered)

    async def ask_approval(self, action: Action) -> bool:
        argv = [str(arg) for arg in action.detail.get("argv", [])]
        if argv and self._remembers(argv):
            return True
        self.echo("")
        self.echo(f"Approval dibutuhkan untuk task {action.task_id or '-'} ({action.kind})")
        if argv:
            self.echo(f"  perintah : {' '.join(argv)}")
        if "cwd" in action.detail:
            self.echo(f"  folder   : {action.detail['cwd']}")
        if not self.isatty():
            self.echo("  stdin bukan terminal interaktif: approval otomatis DITOLAK.")
            return False
        while True:
            answer = await self._read("  [y]a / [n]tidak / [a]selalu untuk prefix ini: ")
            if answer in ("y", "ya"):
                return True
            if answer in ("n", "tidak", ""):
                return False
            if answer == "a":
                prefix = tuple(argv)
                if prefix:
                    self.remembered.add(prefix)
                    if self.policy is not None:
                        self.policy.remembered.add(prefix)
                return True
            self.echo("  Jawab y, n, atau a.")

    async def confirm_merge(self, report: GateReport) -> bool:
        self.echo("")
        self.echo(f"Gate lulus untuk task {report.task_id}.")
        checks = ", ".join(
            f"{name}: {'ok' if ok else 'GAGAL'}" for name, ok in report.checks.items()
        )
        if checks:
            self.echo(f"  pengecekan : {checks}")
        tail = report.output_tail.strip().splitlines()[-_TAIL_LINES:]
        for line in tail:
            self.echo(f"  | {line}")
        if not self.isatty():
            self.echo(
                "  stdin bukan terminal interaktif: merge TIDAK dilakukan (pakai --auto-merge)."
            )
            return False
        answer = await self._read("  Gabungkan ke branch target? [y/n]: ")
        return answer in ("y", "ya")

    async def _read(self, prompt: str) -> str:
        """Read one answer without blocking the event loop (other workers keep running).

        The blocking `read_line` runs in a daemon thread rather than `asyncio.to_thread`: a
        to_thread worker is joined when the program exits, so a Ctrl+C while a question is open
        would hang until someone pressed Enter. A daemon thread is simply abandoned instead.
        End of input counts as "no".
        """
        loop = asyncio.get_running_loop()
        answer: asyncio.Future[str] = loop.create_future()

        def deliver(setter: Callable[[], None]) -> None:
            if not answer.done():  # the question may have been cancelled meanwhile
                setter()

        def read() -> None:
            try:
                text = self.read_line(prompt)
            except EOFError:
                text = ""
            except BaseException as exc:  # handed to the awaiting coroutine
                # Bind now: Python unbinds `exc` when the except block ends.
                loop.call_soon_threadsafe(deliver, lambda e=exc: answer.set_exception(e))
                return
            loop.call_soon_threadsafe(deliver, lambda: answer.set_result(text))

        threading.Thread(target=read, name="caecilia-prompt", daemon=True).start()
        return (await answer).strip().lower()
