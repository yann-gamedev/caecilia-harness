"""API key lookup, storage and redaction (backlog M1-14, ADR-0007)."""

from __future__ import annotations

import keyring
import pytest
from keyring.errors import KeyringError

from caecilia_harness.security import keys
from caecilia_harness.security.keys import (
    MissingApiKeyError,
    UnknownProviderError,
    get_api_key,
    key_source,
    redact,
    set_api_key,
)
from tests.conftest import MemoryKeyring

CANARY = "sk-test-CANARY"


def test_missing_key_explains_how_to_set_it() -> None:
    assert key_source("deepseek") is None
    with pytest.raises(MissingApiKeyError) as info:
        get_api_key("deepseek")
    message = str(info.value)
    assert "caecilia key set deepseek" in message
    assert "DEEPSEEK_API_KEY" in message


def test_env_is_used_when_keyring_is_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DEEPSEEK_API_KEY", CANARY)
    assert key_source("deepseek") == "env"
    assert get_api_key("deepseek") == CANARY


def test_keyring_wins_over_env(
    monkeypatch: pytest.MonkeyPatch, memory_keyring: MemoryKeyring
) -> None:
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-from-env")
    set_api_key("deepseek", f"  {CANARY}\n")
    assert memory_keyring.passwords == {("caecilia-harness", "deepseek"): CANARY}
    assert key_source("deepseek") == "keyring"
    assert get_api_key("deepseek") == CANARY


def test_broken_keyring_falls_back_to_env(monkeypatch: pytest.MonkeyPatch) -> None:
    def broken(service: str, username: str) -> str:
        raise KeyringError("no backend")

    monkeypatch.setattr(keyring, "get_password", broken)
    monkeypatch.setenv("DEEPSEEK_API_KEY", CANARY)
    assert get_api_key("deepseek") == CANARY


def test_redact_replaces_keys_this_process_has_seen(monkeypatch: pytest.MonkeyPatch) -> None:
    text = f"401 Unauthorized: bad key {CANARY} (Bearer {CANARY})"
    assert redact(text) == text  # not handled yet: nothing to redact
    monkeypatch.setenv("DEEPSEEK_API_KEY", CANARY)
    get_api_key("deepseek")
    assert redact(text) == "401 Unauthorized: bad key *** (Bearer ***)"
    assert CANARY not in redact(text)


def test_stored_key_is_redacted_too() -> None:
    set_api_key("deepseek", "sk-other-secret")
    assert redact("x sk-other-secret y") == "x *** y"


def test_missing_key_error_never_contains_a_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DEEPSEEK_API_KEY", "")
    with pytest.raises(MissingApiKeyError) as info:
        get_api_key("deepseek")
    assert "sk-" not in str(info.value)


@pytest.mark.parametrize("call", [get_api_key, key_source])
def test_unknown_provider(call: object) -> None:
    with pytest.raises(UnknownProviderError):
        call("nope")  # type: ignore[operator]
    with pytest.raises(UnknownProviderError):
        set_api_key("nope", "k")


def test_empty_key_is_not_stored(memory_keyring: MemoryKeyring) -> None:
    with pytest.raises(ValueError):
        set_api_key("deepseek", "   ")
    assert memory_keyring.passwords == {}


def test_tests_use_the_memory_keyring(memory_keyring: MemoryKeyring) -> None:
    assert keyring.get_keyring() is memory_keyring
    assert keys.SERVICE == "caecilia-harness"
