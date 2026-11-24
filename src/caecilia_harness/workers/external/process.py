"""One agent CLI process: argv, stdout lines, stderr tail, and a two-step cancel (EXT-01).

`AgentProcess` knows nothing about agent protocols. It starts the program in the task worktree
with its own process group, hands it the task on stdin, yields stdout line by line, keeps the
last bytes of stderr for the log, and can stop the whole tree: a soft signal first, a hard kill
after `grace_s` (docs/architecture.md, "Aturan konkurensi" point 6).
"""

from __future__ import annotations

import asyncio
import contextlib
import ctypes
import os
import signal
import subprocess
import sys
from collections.abc import AsyncGenerator, Mapping, Sequence
from pathlib import Path
from typing import Literal

#: A single stdout line longer than this is refused by asyncio's default reader.
_LINE_LIMIT = 1024 * 1024
#: After a hard kill, how long to wait for the process to be reaped.
_REAP_S = 10.0


class AgentProcessError(RuntimeError):
    """The process lifecycle failed or the process was used before `start()`."""


if sys.platform == "win32":
    # A job object holds the whole tree, so grandchildren die even after the parent exits.
    _KILL_ON_JOB_CLOSE = 0x2000
    _SET_QUOTA = 0x0100
    _TERMINATE = 0x0001
    _EXTENDED_LIMIT_CLASS = 9
    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _kernel32.CreateJobObjectW.restype = ctypes.c_void_p
    _kernel32.CreateJobObjectW.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    _kernel32.SetInformationJobObject.argtypes = [
        ctypes.c_void_p,
        ctypes.c_int,
        ctypes.c_void_p,
        ctypes.c_ulong,
    ]
    _kernel32.AssignProcessToJobObject.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    _kernel32.TerminateJobObject.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
    _kernel32.OpenProcess.restype = ctypes.c_void_p
    _kernel32.OpenProcess.argtypes = [ctypes.c_ulong, ctypes.c_int, ctypes.c_ulong]
    _kernel32.CloseHandle.argtypes = [ctypes.c_void_p]

    class _BasicLimit(ctypes.Structure):
        _fields_ = [
            ("PerProcessUserTimeLimit", ctypes.c_longlong),
            ("PerJobUserTimeLimit", ctypes.c_longlong),
            ("LimitFlags", ctypes.c_ulong),
            ("MinimumWorkingSetSize", ctypes.c_size_t),
            ("MaximumWorkingSetSize", ctypes.c_size_t),
            ("ActiveProcessLimit", ctypes.c_ulong),
            ("Affinity", ctypes.c_size_t),
            ("PriorityClass", ctypes.c_ulong),
            ("SchedulingClass", ctypes.c_ulong),
        ]

    class _IoC(ctypes.Structure):
        _fields_ = [
            ("ReadOperationCount", ctypes.c_ulonglong),
            ("WriteOperationCount", ctypes.c_ulonglong),
            ("OtherOperationCount", ctypes.c_ulonglong),
            ("ReadTransferCount", ctypes.c_ulonglong),
            ("WriteTransferCount", ctypes.c_ulonglong),
            ("OtherTransferCount", ctypes.c_ulonglong),
        ]

    class _ExtendedLimit(ctypes.Structure):
        _fields_ = [
            ("BasicLimitInformation", _BasicLimit),
            ("IoInfo", _IoC),
            ("ProcessMemoryLimit", ctypes.c_size_t),
            ("JobMemoryLimit", ctypes.c_size_t),
            ("PeakProcessMemoryUsed", ctypes.c_size_t),
            ("PeakJobMemoryUsed", ctypes.c_size_t),
        ]

    _kernel32.CreateToolhelp32Snapshot.restype = ctypes.c_void_p
    _kernel32.CreateToolhelp32Snapshot.argtypes = [ctypes.c_ulong, ctypes.c_ulong]
    _kernel32.Thread32First.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    _kernel32.Thread32Next.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    _kernel32.OpenThread.restype = ctypes.c_void_p
    _kernel32.OpenThread.argtypes = [ctypes.c_ulong, ctypes.c_int, ctypes.c_ulong]
    _kernel32.ResumeThread.restype = ctypes.c_ulong
    _kernel32.ResumeThread.argtypes = [ctypes.c_void_p]

    class _ThreadEntry(ctypes.Structure):
        _fields_ = [
            ("dwSize", ctypes.c_ulong),
            ("cntUsage", ctypes.c_ulong),
            ("th32ThreadID", ctypes.c_ulong),
            ("th32OwnerProcessID", ctypes.c_ulong),
            ("tpBasePri", ctypes.c_long),
            ("tpDeltaPri", ctypes.c_long),
            ("dwFlags", ctypes.c_ulong),
        ]

    def _job_create(pid: int) -> int:
        """Contain the suspended process before it can spawn any children; fail closed."""
        job = _kernel32.CreateJobObjectW(None, None)
        if not job:
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            info = _ExtendedLimit()
            info.BasicLimitInformation.LimitFlags = _KILL_ON_JOB_CLOSE
            if not _kernel32.SetInformationJobObject(
                job, _EXTENDED_LIMIT_CLASS, ctypes.byref(info), ctypes.sizeof(info)
            ):
                raise ctypes.WinError(ctypes.get_last_error())
            handle = _kernel32.OpenProcess(_SET_QUOTA | _TERMINATE, False, pid)
            if not handle:
                raise ctypes.WinError(ctypes.get_last_error())
            try:
                if not _kernel32.AssignProcessToJobObject(job, handle):
                    raise ctypes.WinError(ctypes.get_last_error())
            finally:
                _kernel32.CloseHandle(handle)
        except BaseException:
            _kernel32.CloseHandle(job)
            raise
        return int(job)

    def _resume(pid: int) -> None:
        # Popen closes the initial thread handle; recover it via the documented snapshot API.
        snapshot = _kernel32.CreateToolhelp32Snapshot(0x4, 0)  # TH32CS_SNAPTHREAD
        if snapshot == ctypes.c_void_p(-1).value:
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            entry = _ThreadEntry()
            entry.dwSize = ctypes.sizeof(entry)
            found = _kernel32.Thread32First(snapshot, ctypes.byref(entry))
            while found:
                if entry.th32OwnerProcessID == pid:
                    thread = _kernel32.OpenThread(0x2, False, entry.th32ThreadID)
                    if not thread:
                        raise ctypes.WinError(ctypes.get_last_error())
                    try:
                        if _kernel32.ResumeThread(thread) == 0xFFFFFFFF:
                            raise ctypes.WinError(ctypes.get_last_error())
                    finally:
                        _kernel32.CloseHandle(thread)
                    return
                found = _kernel32.Thread32Next(snapshot, ctypes.byref(entry))
            raise AgentProcessError(f"thread process {pid} tidak ditemukan")
        finally:
            _kernel32.CloseHandle(snapshot)

    def _job_kill(job: int) -> None:
        if not _kernel32.TerminateJobObject(job, 1):
            raise AgentProcessError(f"TerminateJobObject gagal: {ctypes.get_last_error()}")

    def _job_close(job: int) -> None:
        if not _kernel32.CloseHandle(job):
            raise ctypes.WinError(ctypes.get_last_error())


class AgentProcess:
    """A running agent CLI program in one worktree."""

    def __init__(
        self,
        argv: Sequence[str],
        cwd: Path,
        env: Mapping[str, str],
        *,
        grace_s: float = 5.0,
        stderr_limit: int = 64_000,
    ) -> None:
        if not argv:
            raise AgentProcessError("argv tidak boleh kosong")
        if stderr_limit <= 0:
            raise ValueError("stderr_limit harus lebih besar dari 0")
        self.argv = list(argv)
        self.cwd = cwd
        self.env = dict(env)
        self.grace_s = grace_s
        self.stderr_limit = stderr_limit
        self._proc: asyncio.subprocess.Process | None = None
        self._started = False
        self._job: int | None = None
        self._stderr = bytearray()
        self._stderr_task: asyncio.Task[None] | None = None
        self._exit_task: asyncio.Task[None] | None = None

    @property
    def pid(self) -> int | None:
        return self._proc.pid if self._proc is not None else None

    @property
    def returncode(self) -> int | None:
        return self._proc.returncode if self._proc is not None else None

    @property
    def stderr_text(self) -> str:
        """The last `stderr_limit` bytes the program wrote, decoded."""
        return bytes(self._stderr).decode("utf-8", errors="replace")

    async def start(self) -> None:
        """Start the program with stdin, stdout and stderr piped and its own process group."""
        if self._started:
            raise AgentProcessError("process sudah dijalankan")
        self._started = True  # set before the first await so a concurrent start() loses
        creationflags = 0
        if sys.platform == "win32":
            creationflags = subprocess.CREATE_NEW_PROCESS_GROUP | 0x4  # CREATE_SUSPENDED
        try:
            self._proc = await asyncio.create_subprocess_exec(
                *self.argv,
                cwd=str(self.cwd),
                env=self.env,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                limit=_LINE_LIMIT,
                creationflags=creationflags,
                start_new_session=sys.platform != "win32",
            )
        except BaseException:
            self._started = False
            raise
        self._stderr_task = asyncio.create_task(self._drain_stderr())
        if sys.platform == "win32":
            try:
                self._job = _job_create(self._proc.pid)
                _resume(self._proc.pid)
            except BaseException:
                self._proc.kill()
                await self._reap()
                await self.close()
                raise
        self._exit_task = asyncio.create_task(self._watch_exit())

    async def _watch_exit(self) -> None:
        assert self._proc is not None  # noqa: S101
        # Process.wait() may wait for inherited pipes; returncode observes OS exit independently.
        while self._proc.returncode is None:  # noqa: ASYNC110 - no public OS-exit-only awaitable
            await asyncio.sleep(0.02)
        await self._kill_tree()

    async def send(self, text: str) -> None:
        """Write `text` to stdin and close it: the agent reads the task until EOF."""
        if self._proc is None or self._proc.stdin is None:
            raise AgentProcessError("process belum dijalankan")
        try:
            self._proc.stdin.write(text.encode("utf-8"))
            await self._proc.stdin.drain()
        except (BrokenPipeError, ConnectionResetError):
            pass  # the agent exited before reading its task; the exit code tells the story
        finally:
            with contextlib.suppress(BrokenPipeError, ConnectionResetError):
                self._proc.stdin.close()
                await self._proc.stdin.wait_closed()

    async def lines(self) -> AsyncGenerator[str, None]:
        """Yield stdout lines (without the line ending) until the stream ends."""
        if self._proc is None or self._proc.stdout is None:
            raise AgentProcessError("process belum dijalankan")
        assert self._exit_task is not None  # noqa: S101 - start() installs the exit watcher
        while True:
            reader = asyncio.create_task(self._proc.stdout.readline())
            try:
                done, _ = await asyncio.wait(
                    {reader, self._exit_task}, return_when=asyncio.FIRST_COMPLETED
                )
                if self._exit_task in done:
                    await self._exit_task  # surface cleanup failures before waiting for pipe EOF
                    raw = await asyncio.wait_for(reader, _REAP_S)
                else:
                    raw = await reader
            finally:
                if not reader.done():
                    reader.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await reader
            if not raw:
                return
            yield raw.decode("utf-8", errors="replace").rstrip("\r\n")

    async def wait(self) -> int:
        if self._proc is None:
            raise AgentProcessError("process belum dijalankan")
        if self._exit_task is not None:
            await asyncio.shield(self._exit_task)
        await self._reap()
        assert self._proc.returncode is not None  # noqa: S101 - _reap() waits for OS exit
        return self._proc.returncode

    async def close(self) -> None:
        """Release the tree, then wait (bounded) for the stderr reader to finish."""
        self._close_job()
        if self._exit_task is not None:
            try:
                await asyncio.wait_for(asyncio.shield(self._exit_task), _REAP_S)
            except TimeoutError as exc:
                self._exit_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await self._exit_task
                raise AgentProcessError("process belum berhenti saat close") from exc
        task = self._stderr_task
        if task is not None:
            try:
                await asyncio.wait_for(asyncio.shield(task), _REAP_S)
            except TimeoutError as exc:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
                raise AgentProcessError("stderr tidak selesai setelah process berhenti") from exc
            finally:
                if task.done():
                    self._stderr_task = None

    async def cancel(self, mode: Literal["soft", "hard"]) -> None:
        """Soft: one signal, then a hard kill after `grace_s`. Hard: kill the whole tree now."""
        if self._proc is None:
            return
        if mode == "soft" and self._proc.returncode is None and self._signal_soft():
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._proc.wait(), self.grace_s)
        # Always kill the tree: a parent may have exited softly while its children stay alive.
        await self._kill_tree()
        await self._reap()

    def _signal_soft(self) -> bool:
        """Ask the program to stop the way Ctrl+C/Ctrl+Break would. False if not possible."""
        if self._proc is None:
            return False
        try:
            if sys.platform == "win32":
                os.kill(self._proc.pid, signal.CTRL_BREAK_EVENT)
            else:
                os.killpg(self._proc.pid, signal.SIGINT)
        except (OSError, ProcessLookupError):
            return False
        return True

    async def _reap(self) -> None:
        if self._proc is None:
            return
        try:
            await asyncio.wait_for(self._proc.wait(), _REAP_S)
        except TimeoutError as exc:
            raise AgentProcessError("process tidak selesai setelah kill") from exc

    async def _kill_tree(self) -> None:
        """Kill the process and every process it started, even ones the parent left behind."""
        if self._proc is None:
            return
        if sys.platform == "win32":
            if self._job is not None:
                _job_kill(self._job)
        else:
            # The child leads its own session, so its pgid equals its pid. The group id stays
            # valid while any member lives, which is why this works after the leader exits.
            with contextlib.suppress(ProcessLookupError):
                os.killpg(self._proc.pid, signal.SIGKILL)
        with contextlib.suppress(ProcessLookupError):
            self._proc.kill()

    def _close_job(self) -> None:
        if sys.platform == "win32" and self._job is not None:
            _job_close(self._job)
            self._job = None

    async def _drain_stderr(self) -> None:
        assert self._proc is not None and self._proc.stderr is not None  # noqa: S101
        while chunk := await self._proc.stderr.read(65_536):
            self._stderr += chunk
            if len(self._stderr) > self.stderr_limit:
                del self._stderr[: -self.stderr_limit]


__all__ = ["AgentProcess", "AgentProcessError"]
