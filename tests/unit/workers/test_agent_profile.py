"""Agent profiles and version detection (EXT-01, ADR-0018)."""

from __future__ import annotations

import sys

import pytest

from caecilia_harness.workers.external.profile import (
    AGENT_PROFILES,
    AgentProfileError,
    detect_agent,
    fake_agent_script,
    parse_version,
    profile_for,
)


def test_registry_has_every_worker_name() -> None:
    assert set(AGENT_PROFILES) == {"claude-code", "codex", "gemini-cli", "fake-agent"}


def test_unknown_agent_lists_the_known_ones() -> None:
    with pytest.raises(AgentProfileError, match="agent tidak dikenal") as info:
        profile_for("other")
    assert "claude-code" in str(info.value)


@pytest.mark.parametrize("name", ["codex", "gemini-cli"])
def test_subscription_waits_for_the_owner_decision(name: str) -> None:
    with pytest.raises(AgentProfileError, match="D14"):
        profile_for(name, auth="subscription")
    assert profile_for(name, auth="api-key").auth == "api-key"


def test_claude_code_may_use_its_own_login() -> None:
    profile = profile_for("claude-code")
    assert profile.auth == "subscription"
    assert "legal-and-compliance" in profile.terms_url


def test_fake_agent_profile_points_at_the_fixture() -> None:
    profile = profile_for("fake-agent")
    assert profile.binary == sys.executable
    assert profile.extra_args == (str(fake_agent_script()),)
    assert fake_agent_script().is_file()


def test_fake_agent_is_detected_and_compatible() -> None:
    detection = detect_agent(profile_for("fake-agent"))
    assert detection.found and detection.compatible
    assert detection.version == "0.1.0"


def test_missing_binary_is_not_found(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PATH", "")
    detection = detect_agent(profile_for("claude-code"))
    assert not detection.found


def test_parse_version_takes_the_first_number() -> None:
    assert parse_version("claude 1.2.3 (build 9)") == (1, 2, 3)
    assert parse_version("codex v0.9") == (0, 9, 0)
    assert parse_version("no version here") is None


def test_config_overrides_are_applied() -> None:
    profile = profile_for(
        "claude-code", model="sonnet", extra_args=("--permission-mode", "default")
    )
    assert profile.model == "sonnet"
    assert profile.extra_args == ("--permission-mode", "default")
