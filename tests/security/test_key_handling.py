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

#: Every provider whose key the harness reads, with the environment variable for it.
PROVIDERS = [("deepseek", "DEEPSEEK_API_KEY"), ("gemini", "GEMINI_API_KEY")]


def test_known_providers() -> None:
    assert dict(PROVIDERS) == keys.ENV_VARS


@pytest.mark.parametrize(("provider", "env_var"), PROVIDERS)
def test_missing_key_explains_how_to_set_it(provider: str, env_var: str) -> None:
    assert key_source(provider) is None
    with pytest.raises(MissingApiKeyError) as info:
        get_api_key(provider)
    message = str(info.value)
    assert f"caecilia key set {provider}" in message
    assert env_var in message


@pytest.mark.parametrize(("provider", "env_var"), PROVIDERS)
def test_env_is_used_when_keyring_is_empty(
    monkeypatch: pytest.MonkeyPatch, provider: str, env_var: str
) -> None:
    monkeypatch.setenv(env_var, CANARY)
    assert key_source(provider) == "env"
    assert get_api_key(provider) == CANARY


@pytest.mark.parametrize(("provider", "env_var"), PROVIDERS)
def test_keyring_wins_over_env(
    monkeypatch: pytest.MonkeyPatch, memory_keyring: MemoryKeyring, provider: str, env_var: str
) -> None:
    monkeypatch.setenv(env_var, "sk-from-env")
    set_api_key(provider, f"  {CANARY}\n")
    assert memory_keyring.passwords == {("caecilia-harness", provider): CANARY}
    assert key_source(provider) == "keyring"
    assert get_api_key(provider) == CANARY


def test_providers_never_share_a_key(
    monkeypatch: pytest.MonkeyPatch, memory_keyring: MemoryKeyring
) -> None:
    monkeypatch.setenv("GEMINI_API_KEY", "gemini-from-env")
    set_api_key("deepseek", "deepseek-in-keyring")
    assert get_api_key("gemini") == "gemini-from-env"
    assert get_api_key("deepseek") == "deepseek-in-keyring"
    monkeypatch.delenv("GEMINI_API_KEY")
    with pytest.raises(MissingApiKeyError):
        get_api_key("gemini")  # DeepSeek's key is never used for Gemini


@pytest.mark.parametrize(("provider", "env_var"), PROVIDERS)
def test_broken_keyring_falls_back_to_env(
    monkeypatch: pytest.MonkeyPatch, provider: str, env_var: str
) -> None:
    def broken(service: str, username: str) -> str:
        raise KeyringError("no backend")

    monkeypatch.setattr(keyring, "get_password", broken)
    monkeypatch.setenv(env_var, CANARY)
    assert get_api_key(provider) == CANARY


@pytest.mark.parametrize(("provider", "env_var"), PROVIDERS)
def test_redact_replaces_keys_this_process_has_seen(
    monkeypatch: pytest.MonkeyPatch, provider: str, env_var: str
) -> None:
    text = f"401 Unauthorized: bad key {CANARY} (Bearer {CANARY})"
    assert redact(text) == text  # not handled yet: nothing to redact
    monkeypatch.setenv(env_var, CANARY)
    get_api_key(provider)
    assert redact(text) == "401 Unauthorized: bad key *** (Bearer ***)"
    assert CANARY not in redact(text)


@pytest.mark.parametrize("provider", ["deepseek", "gemini"])
def test_stored_key_is_redacted_too(provider: str) -> None:
    set_api_key(provider, "sk-other-secret")
    assert redact("x sk-other-secret y") == "x *** y"


@pytest.mark.parametrize(("provider", "env_var"), PROVIDERS)
def test_missing_key_error_never_contains_a_key(
    monkeypatch: pytest.MonkeyPatch, provider: str, env_var: str
) -> None:
    monkeypatch.setenv(env_var, "")
    with pytest.raises(MissingApiKeyError) as info:
        get_api_key(provider)
    assert "sk-" not in str(info.value)


@pytest.mark.parametrize("call", [get_api_key, key_source])
def test_unknown_provider(call: object) -> None:
    with pytest.raises(UnknownProviderError):
        call("nope")  # type: ignore[operator]
    with pytest.raises(UnknownProviderError):
        set_api_key("nope", "k")


@pytest.mark.parametrize("provider", ["deepseek", "gemini"])
def test_empty_key_is_not_stored(memory_keyring: MemoryKeyring, provider: str) -> None:
    with pytest.raises(ValueError):
        set_api_key(provider, "   ")
    assert memory_keyring.passwords == {}


def test_tests_use_the_memory_keyring(memory_keyring: MemoryKeyring) -> None:
    assert keyring.get_keyring() is memory_keyring
    assert keys.SERVICE == "caecilia-harness"
