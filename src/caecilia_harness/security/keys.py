"""API keys for model providers (ADR-0007: API key only, kept in the OS keychain).

Lookup order: OS keychain (`keyring`), then the provider's environment variable (development).
A key is never printed, logged or stored in an event; `redact()` scrubs any key this process
has seen from text that is about to leave the process.
"""

from __future__ import annotations

import os

import keyring
from keyring.errors import KeyringError

SERVICE = "caecilia-harness"

#: provider id -> environment variable accepted as a fallback. A key listed here can be stored,
#: read and redacted; whether a model provider uses it is decided in bootstrap (D2: DeepSeek).
ENV_VARS: dict[str, str] = {
    "deepseek": "DEEPSEEK_API_KEY",
    "gemini": "GEMINI_API_KEY",
    # Keys for external agent CLIs with `auth = "api-key"` (EXT-01, ADR-0018).
    "anthropic": "ANTHROPIC_API_KEY",
    "openai": "OPENAI_API_KEY",
}

_known_keys: set[str] = set()


class MissingApiKeyError(Exception):
    """No API key is stored for the provider."""


class UnknownProviderError(ValueError):
    """The provider id is not one the harness knows."""


def _check_provider(provider: str) -> None:
    if provider not in ENV_VARS:
        raise UnknownProviderError(
            f"provider tidak dikenal: {provider!r} (tersedia: {', '.join(sorted(ENV_VARS))})"
        )


def _from_keyring(provider: str) -> str | None:
    try:
        return keyring.get_password(SERVICE, provider) or None
    except KeyringError:
        return None  # no usable keychain backend: fall back to the environment


def key_source(provider: str) -> str | None:
    """Where the key would come from: "keyring", "env", or None if there is none."""
    _check_provider(provider)
    if _from_keyring(provider):
        return "keyring"
    if os.environ.get(ENV_VARS[provider]):
        return "env"
    return None


def get_api_key(provider: str) -> str:
    """Return the provider's API key, or raise `MissingApiKeyError` with setup instructions."""
    _check_provider(provider)
    key = _from_keyring(provider) or os.environ.get(ENV_VARS[provider])
    if not key:
        raise MissingApiKeyError(
            f"API key untuk {provider} belum ada. Simpan dengan `caecilia key set {provider}` "
            f"(disimpan di keychain OS), atau set variabel env {ENV_VARS[provider]}."
        )
    _known_keys.add(key)
    return key


def set_api_key(provider: str, key: str) -> None:
    """Store the key in the OS keychain."""
    _check_provider(provider)
    key = key.strip()
    if not key:
        raise ValueError("API key kosong")
    keyring.set_password(SERVICE, provider, key)
    _known_keys.add(key)


def redact(text: str) -> str:
    """Replace every API key this process has handled with `***`."""
    for key in _known_keys:
        text = text.replace(key, "***")
    return text
