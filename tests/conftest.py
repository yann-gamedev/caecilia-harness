from __future__ import annotations

import socket
from collections.abc import Iterator
from pathlib import Path

import keyring
import keyring.backend
import pytest

from caecilia_harness.security import keys

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


class MemoryKeyring(keyring.backend.KeyringBackend):
    """In-memory keyring so tests never read or write the real OS keychain."""

    priority = 1.0  # type: ignore[assignment]

    def __init__(self) -> None:
        super().__init__()
        self.passwords: dict[tuple[str, str], str] = {}

    def get_password(self, service: str, username: str) -> str | None:
        return self.passwords.get((service, username))

    def set_password(self, service: str, username: str, password: str) -> None:
        self.passwords[(service, username)] = password

    def delete_password(self, service: str, username: str) -> None:
        self.passwords.pop((service, username), None)


@pytest.fixture(autouse=True)
def memory_keyring(monkeypatch: pytest.MonkeyPatch) -> Iterator[MemoryKeyring]:
    """Every test gets an empty fake keychain and no provider key in the environment."""
    previous = keyring.get_keyring()
    backend = MemoryKeyring()
    keyring.set_keyring(backend)
    for env_var in keys.ENV_VARS.values():
        monkeypatch.delenv(env_var, raising=False)
    monkeypatch.setattr(keys, "_known_keys", set())
    try:
        yield backend
    finally:
        keyring.set_keyring(previous)
