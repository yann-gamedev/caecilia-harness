from __future__ import annotations

import socket
from pathlib import Path

import pytest

_LOOPBACK = {"127.0.0.1", "::1", "localhost"}


@pytest.fixture(autouse=True)
def isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Every test gets its own CAECILIA_HOME so nothing touches the real one."""
    home = tmp_path / "caecilia-home"
    monkeypatch.setenv("CAECILIA_HOME", str(home))
    return home


@pytest.fixture(autouse=True)
def no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """Tests must never reach the network (AGENTS.md). Loopback and non-IP sockets stay allowed."""
    real_connect = socket.socket.connect

    def guarded(self: socket.socket, address: object) -> None:
        if self.family in (socket.AF_INET, socket.AF_INET6):
            host = address[0] if isinstance(address, tuple) else address
            if host not in _LOOPBACK:
                raise RuntimeError(f"network access blocked in tests: {address!r}")
        real_connect(self, address)  # type: ignore[arg-type]

    monkeypatch.setattr(socket.socket, "connect", guarded)
