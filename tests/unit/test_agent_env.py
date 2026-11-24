"""Environment of an external agent process (EXT-01, ADR-0018 point 2)."""

from __future__ import annotations

import pytest

from caecilia_harness.security.env import AgentEnvError, agent_env, scrub_env
from caecilia_harness.workers.external.profile import AgentProfile

PARENT = {
    "PATH": "/usr/bin",
    "HOME": "/home/person",
    "LANG": "id_ID.UTF-8",
    "ANTHROPIC_API_KEY": "sk-ant-parent",
    "OPENAI_API_KEY": "sk-openai-parent",
    "GEMINI_API_KEY": "sk-gemini-parent",
    "DEEPSEEK_API_KEY": "sk-deepseek-parent",
    "SOME_TOKEN": "secret",
}


def _profile(name: str, auth: str) -> AgentProfile:
    return AgentProfile(name=name, binary="agent", min_version="0.1.0", auth=auth)  # type: ignore[arg-type]


@pytest.mark.parametrize("name", ["claude-code", "codex", "gemini-cli"])
def test_subscription_removes_every_vendor_key(name: str) -> None:
    env = agent_env(_profile(name, "subscription"), PARENT)
    assert not {"ANTHROPIC_API_KEY", "OPENAI_API_KEY", "GEMINI_API_KEY"} & set(env)
    assert env["PATH"] == "/usr/bin" and env["HOME"] == "/home/person"
    assert env["LANG"] == "id_ID.UTF-8"
    assert "SOME_TOKEN" not in env


@pytest.mark.parametrize(
    ("name", "variable"),
    [
        ("claude-code", "ANTHROPIC_API_KEY"),
        ("codex", "OPENAI_API_KEY"),
        ("gemini-cli", "GEMINI_API_KEY"),
    ],
)
def test_api_key_mode_passes_exactly_the_vendors_variable(name: str, variable: str) -> None:
    env = agent_env(_profile(name, "api-key"), PARENT, "sk-injected")
    assert env[variable] == "sk-injected"
    assert set(env) & {"ANTHROPIC_API_KEY", "OPENAI_API_KEY", "GEMINI_API_KEY"} == {variable}
    assert "DEEPSEEK_API_KEY" not in env


def test_api_key_mode_without_a_key_is_an_error() -> None:
    with pytest.raises(AgentEnvError, match="belum ada"):
        agent_env(_profile("codex", "api-key"), PARENT)


def test_agent_without_an_official_key_variable_is_refused() -> None:
    with pytest.raises(AgentEnvError, match="variabel API key resmi"):
        agent_env(_profile("fake-agent", "api-key"), PARENT, "sk-x")


def test_parent_environment_is_not_modified() -> None:
    before = dict(PARENT)
    agent_env(_profile("claude-code", "subscription"), PARENT)
    assert before == PARENT


def test_scrub_still_runs_before_the_key_is_added() -> None:
    env = agent_env(_profile("codex", "api-key"), PARENT, "sk-injected")
    assert scrub_env(env)["PATH"] == "/usr/bin"
