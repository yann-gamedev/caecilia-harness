"""Secret scrubbing of the exec environment (backlog M1-06)."""

from __future__ import annotations

import pytest

from caecilia_harness.security.env import scrub_env


def test_canary_and_api_keys_are_removed() -> None:
    env = {
        "CAECILIA_CANARY_TOKEN": "xyz",
        "MY_API_KEY": "k",
        "PATH": "/usr/bin",
        "LANG": "C.UTF-8",
    }
    assert scrub_env(env) == {"PATH": "/usr/bin", "LANG": "C.UTF-8"}


@pytest.mark.parametrize(
    "name",
    [
        "ANTHROPIC_API_KEY",
        "github_token",
        "DB_Password",
        "PGPASSWD",
        "AWS_SECRET_ACCESS_KEY",
        "GOOGLE_APPLICATION_CREDENTIALS",
        "NPM_AUTH",
        "SESSION_COOKIE",
        "SSH_AUTH_SOCK",
    ],
)
def test_secret_like_names_are_removed_case_insensitively(name: str) -> None:
    assert scrub_env({name: "s"}) == {}


@pytest.mark.parametrize(
    "name", ["PATH", "Path", "SYSTEMROOT", "SystemRoot", "COMSPEC", "TEMP", "TMP", "HOME"]
)
def test_essential_os_variables_are_kept(name: str) -> None:
    assert scrub_env({name: "v"}) == {name: "v"}


def test_input_is_not_modified() -> None:
    env = {"X_TOKEN": "t"}
    scrub_env(env)
    assert env == {"X_TOKEN": "t"}
