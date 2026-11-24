"""Real subprocess lifecycle checks, without invoking an agent or the network."""

from __future__ import annotations

import asyncio
import contextlib
import ctypes
import os
import signal
import sys
from pathlib import Path
from typing import Literal

import pytest

import caecilia_harness.workers.external.process as process_module
from caecilia_harness.workers.external.process import AgentProcess, AgentProcessError

_TREE = """
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

depth = int(sys.argv[1])
sig = signal.SIGBREAK if sys.platform == 'win32' else signal.SIGINT

def stop(*_):
    Path('soft-seen').write_text('yes')
    os._exit(0)

signal.signal(sig, signal.SIG_IGN if depth == 0 else stop)
if depth:
    subprocess.Popen([sys.executable, '-u', __file__, str(depth - 1)])
print(f'stderr {depth}', file=sys.stderr, flush=True)
print(f'{os.getpid()} {depth}', flush=True)
while True:
    if depth == 2 and Path('exit-parent').exists():
        os._exit(0)
    time.sleep(0.02)
"""


def _process(tmp_path: Path, code: str, *, stderr_limit: int = 64_000) -> AgentProcess:
    return AgentProcess(
        [sys.executable, "-u", "-c", code], tmp_path, os.environ, stderr_limit=stderr_limit
    )


def _alive(pid: int) -> bool:
    if sys.platform == "win32":
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.OpenProcess.restype = ctypes.c_void_p
        kernel.OpenProcess.argtypes = [ctypes.c_ulong, ctypes.c_int, ctypes.c_ulong]
        kernel.GetExitCodeProcess.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_ulong)]
        kernel.CloseHandle.argtypes = [ctypes.c_void_p]
        handle = kernel.OpenProcess(0x1000, False, pid)
        if not handle:
            return False
        try:
            code = ctypes.c_ulong()
            if not kernel.GetExitCodeProcess(handle, ctypes.byref(code)):
                raise ctypes.WinError(ctypes.get_last_error())
            return code.value == 259  # STILL_ACTIVE
        finally:
            kernel.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    # Linux container PID 1 may leave terminated grandchildren as zombies.
    stat = Path(f"/proc/{pid}/stat")
    try:
        return stat.read_text().split(")", 1)[1].split()[0] != "Z"
    except FileNotFoundError:
        return True  # /proc is unavailable on other POSIX platforms; kill(0) succeeded


@pytest.mark.parametrize("mode", ["soft", "hard"])
@pytest.mark.parametrize("parent_exits_first", [False, True])
async def test_cancel_kills_tree_even_when_parent_exits_soft(
    tmp_path: Path, mode: Literal["soft", "hard"], parent_exits_first: bool
) -> None:
    script = tmp_path / "tree.py"
    script.write_text(_TREE)
    proc = AgentProcess([sys.executable, "-u", str(script), "2"], tmp_path, os.environ, grace_s=0.2)
    pids: list[int] = []
    await proc.start()
    try:
        async with asyncio.timeout(10):
            lines = proc.lines()
            for _ in range(3):
                pids.append(int((await anext(lines)).split()[0]))
            await lines.aclose()
            if parent_exits_first:
                (tmp_path / "exit-parent").touch()
                while proc.returncode is None:  # noqa: ASYNC110 - observe leader exit before cancel
                    await asyncio.sleep(0.02)
            await proc.cancel(mode)
            await proc.close()
            while any(_alive(pid) for pid in pids):  # noqa: ASYNC110 - poll real OS process exit
                await asyncio.sleep(0.02)
        assert proc.returncode is not None
        if mode == "soft" and not parent_exits_first and sys.platform != "win32":
            assert (tmp_path / "soft-seen").exists()
        await proc.cancel("hard")  # repeated cleanup is harmless
    finally:
        await proc.cancel("hard")
        if sys.platform == "win32":
            for pid in pids:
                if _alive(pid):
                    killer = await asyncio.create_subprocess_exec(
                        "taskkill",
                        "/F",
                        "/T",
                        "/PID",
                        str(pid),
                        stdout=asyncio.subprocess.DEVNULL,
                        stderr=asyncio.subprocess.DEVNULL,
                    )
                    await killer.wait()
        else:
            for pid in pids:
                with contextlib.suppress(ProcessLookupError):
                    os.kill(pid, signal.SIGKILL)
        await proc.close()


async def test_normal_parent_exit_closes_inherited_pipes_and_kills_tree(tmp_path: Path) -> None:
    script = tmp_path / "tree.py"
    script.write_text(_TREE)
    proc = AgentProcess([sys.executable, "-u", str(script), "2"], tmp_path, os.environ)
    pids: list[int] = []
    await proc.start()
    try:
        async with asyncio.timeout(5):
            lines = proc.lines()
            for _ in range(3):
                pids.append(int((await anext(lines)).split()[0]))
            (tmp_path / "exit-parent").touch()
            # ExternalAgentWorker consumes stdout EOF before wait()/close(), without cancel().
            waiting = asyncio.create_task(proc.wait())
            assert [line async for line in lines] == []
            assert await waiting == 0
            await proc.close()
            assert sorted(proc.stderr_text.splitlines()) == ["stderr 0", "stderr 1", "stderr 2"]
            while any(_alive(pid) for pid in pids):  # noqa: ASYNC110 - poll real OS process exit
                await asyncio.sleep(0.02)
    finally:
        await proc.cancel("hard")
        await proc.close()


async def test_stdin_stdout_and_stderr_tail(tmp_path: Path) -> None:
    proc = _process(
        tmp_path,
        "import os, sys; print(sys.stdin.read()); os.write(2, b'x' * 200000 + b'\\xfftail')",
        stderr_limit=8,
    )
    await proc.start()
    await proc.send("hello")
    assert [line async for line in proc.lines()] == ["hello"]
    assert await asyncio.wait_for(proc.wait(), 10) == 0
    await proc.close()
    assert proc.stderr_text == "xxx\ufffdtail"
    await proc.close()


@pytest.mark.parametrize("limit", [0, -1])
def test_stderr_limit_must_be_positive(tmp_path: Path, limit: int) -> None:
    with pytest.raises(ValueError, match="stderr_limit"):
        _process(tmp_path, "", stderr_limit=limit)


async def test_start_twice_including_concurrent_start(tmp_path: Path) -> None:
    proc = _process(tmp_path, "import time; time.sleep(60)")
    results = await asyncio.gather(proc.start(), proc.start(), return_exceptions=True)
    try:
        assert sum(isinstance(result, AgentProcessError) for result in results) == 1
        with pytest.raises(AgentProcessError, match="sudah"):
            await proc.start()
    finally:
        await proc.cancel("hard")
        await proc.close()


async def test_unstarted_process_guard(tmp_path: Path) -> None:
    proc = _process(tmp_path, "")
    with pytest.raises(AgentProcessError, match="belum"):
        await proc.wait()
    with pytest.raises(AgentProcessError, match="belum"):
        await proc.send("hello")
    with pytest.raises(AgentProcessError, match="belum"):
        await anext(proc.lines())
    await proc.cancel("hard")
    await proc.close()


async def test_reap_timeout_is_reported(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    proc = _process(tmp_path, "import time; print('ready', flush=True); time.sleep(60)")
    await proc.start()
    try:
        assert await anext(proc.lines()) == "ready"
        monkeypatch.setattr(process_module, "_REAP_S", 0.02)
        with pytest.raises(AgentProcessError, match="setelah kill"):
            await proc._reap()
    finally:
        monkeypatch.setattr(process_module, "_REAP_S", 10.0)
        await proc.cancel("hard")
        await proc.close()


async def test_stderr_drain_failure_is_reported(tmp_path: Path) -> None:
    class BrokenReader:
        async def read(self, size: int) -> bytes:
            raise OSError("stderr broken")

    proc = _process(tmp_path, "")
    await proc.start()
    await proc.wait()
    await proc.close()
    assert proc._proc is not None
    proc._proc.stderr = BrokenReader()  # type: ignore[assignment]
    proc._stderr_task = asyncio.create_task(proc._drain_stderr())
    with pytest.raises(OSError, match="stderr broken"):
        await proc.close()
