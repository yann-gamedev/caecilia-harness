"""Driving a run in the terminal: live event lines and Ctrl+C (`run`, `-p`, the REPL)."""

from __future__ import annotations

import asyncio
import contextlib
import signal
from collections.abc import Awaitable, Callable, Iterator

import typer

from caecilia_harness.bootstrap import PreparedRun
from caecilia_harness.interfaces.render import format_event

Echo = Callable[[str], None]
_POLL_S = 0.2


def _echo(text: str) -> None:
    typer.echo(text)


@contextlib.contextmanager
def on_sigint(callback: Callable[[], None]) -> Iterator[None]:
    """Call `callback` on the event loop for every Ctrl+C while the block runs.

    Outside the main thread signals cannot be handled, so Ctrl+C is left alone there.
    Windows uses plain SIGINT.
    """
    loop = asyncio.get_running_loop()
    try:
        previous = signal.signal(
            signal.SIGINT, lambda signum, frame: loop.call_soon_threadsafe(callback)
        )
    except ValueError:  # not in the main thread
        yield
        return
    try:
        yield
    finally:
        signal.signal(signal.SIGINT, previous)


@contextlib.contextmanager
def ctrl_c(prepared: PreparedRun, running: asyncio.Task[str], echo: Echo = _echo) -> Iterator[None]:
    """First Ctrl+C cancels workers softly; the second stops the whole run.

    Stopping the run also works while a question is waiting for an answer: the scheduler then
    cancels every worker itself.
    """
    loop = asyncio.get_running_loop()
    presses = 0
    cancelling: set[asyncio.Task[None]] = set()  # keep references until the tasks finish

    def pressed() -> None:
        nonlocal presses
        presses += 1
        if presses == 1:
            echo("\nMembatalkan dengan halus... tekan Ctrl+C sekali lagi untuk paksa.")
            task = loop.create_task(prepared.orchestrator.cancel_all("soft"))
            cancelling.add(task)
            task.add_done_callback(cancelling.discard)
        else:
            echo("\nMembatalkan paksa.")
            running.cancel()

    with on_sigint(pressed):
        yield


async def drive(
    start: Awaitable[PreparedRun],
    plain: bool,
    echo: Echo = _echo,
    show_events: bool = True,
) -> str:
    """Execute a prepared run, print its events as they happen, and return its final status.

    A second Ctrl+C stops the run and returns "cancelled" (continue with `resume`); a
    cancellation from outside propagates. The run is always closed (writer lock released).
    """
    prepared = await start
    try:
        echo(f"Run {prepared.run_id} berjalan.")

        def flush() -> None:
            for event in prepared.new_events():
                if show_events:
                    echo(format_event(event, plain, run_id=prepared.run_id))

        async def printer() -> None:
            while True:
                flush()
                await asyncio.sleep(_POLL_S)

        printing = asyncio.create_task(printer())
        running = asyncio.create_task(prepared.execute())
        try:
            with ctrl_c(prepared, running, echo):
                try:
                    status = await running
                except asyncio.CancelledError:
                    current = asyncio.current_task()
                    if current is not None and current.cancelling():
                        raise  # caller cancellation must propagate, unlike second Ctrl+C
                    echo(f"Run {prepared.run_id} dihentikan. Lanjutkan dengan `caecilia resume`.")
                    status = "cancelled"
        finally:
            printing.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await printing
            flush()
        echo(f"Run {prepared.run_id} selesai: {status}")
        return status
    finally:
        prepared.close()
