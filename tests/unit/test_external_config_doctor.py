"""External agent configuration and doctor detection (EXT-01, ADR-0018)."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from caecilia_harness.interfaces import doctor
from caecilia_harness.project_config import (
    AgentConfig,
    CliValues,
    ConfigError,
    ProjectConfig,
    dump_config,
    load_config,
    parse_config,
    resolve_options,
    write_config,
)
from caecilia_harness.workers.external.profile import AgentProfileError, profile_for


def test_agents_config_round_trips_and_reaches_run_options(tmp_path: Path) -> None:
    config = ProjectConfig(
        worker="claude-code",
        agents={
            "claude-code": AgentConfig(
                auth="subscription",
                model="sonnet",
                extra_args=["--directory", 'C:\\project with spaces\\"quoted"'],
            ),
            "codex": AgentConfig(auth="api-key", model="codex-model"),
            "gemini-cli": AgentConfig(auth="api-key"),
            "fake-agent": AgentConfig(),
        },
    )
    text = dump_config(config)
    assert "[agents.claude-code]" in text
    assert parse_config(text) == config
    assert load_config(write_config(tmp_path, config)) == config
    options = resolve_options(CliValues(), config)
    assert options.worker == "claude-code"
    assert options.agents == config.agents
    assert options.agents is not config.agents


@pytest.mark.parametrize(
    "field", ["api_key", "ANTHROPIC_API_KEY", "token", "secret", "password", "passwd", "credential"]
)
def test_nested_agent_secret_fields_are_rejected_without_echoing_values(field: str) -> None:
    with pytest.raises(ConfigError) as info:
        parse_config(f'[agents.claude-code]\n{field} = "sk-nested-CANARY"\n')
    message = str(info.value)
    assert "agents.claude-code" in message
    assert field in message
    assert "caecilia key set" in message
    assert "sk-nested-CANARY" not in message


def test_doctor_reports_unsupported_fake_api_key_without_crashing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = parse_config('worker = "fake-agent"\n[agents.fake-agent]\nauth = "api-key"\n')
    monkeypatch.setattr(doctor, "project_settings", lambda cwd: (tmp_path, config))
    checks = doctor.check_project(tmp_path)
    assert any(
        check.level == "fail" and "tidak mendukung API key" in check.detail for check in checks
    )


def test_unknown_agent_config_is_rejected() -> None:
    with pytest.raises(ConfigError, match="agent tidak dikenal: unknown-agent"):
        parse_config('[agents.unknown-agent]\nauth = "api-key"\n')


def test_unknown_agent_profile_is_rejected() -> None:
    with pytest.raises(AgentProfileError, match="agent tidak dikenal: 'unknown-agent'"):
        profile_for("unknown-agent")


@pytest.mark.parametrize("worker", ["fake", "claude-code"])
@pytest.mark.parametrize("version", [None, "1.2.3", "0.9.9"], ids=["missing", "present", "old"])
def test_doctor_detects_configured_agent_using_only_version_command(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, worker: str, version: str | None
) -> None:
    config = parse_config(f'worker = "{worker}"\n[agents.claude-code]\nauth = "subscription"\n')
    monkeypatch.setattr(doctor, "project_settings", lambda cwd: (tmp_path, config))
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    monkeypatch.setenv("PATH", str(bin_dir))
    calls = tmp_path / "agent-calls.txt"
    binary = bin_dir / ("claude.cmd" if os.name == "nt" else "claude")
    if version is not None:
        if os.name == "nt":
            script = f'@echo off\n>>"{calls}" echo %*\necho Claude Code {version}\n'
        else:
            script = f'#!/bin/sh\nprintf "%s\\n" "$*" >> "{calls}"\necho "Claude Code {version}"\n'
        binary.write_text(script, encoding="utf-8", newline="\n")
        if os.name != "nt":
            binary.chmod(0o755)

    checks = doctor.check_project(tmp_path)
    agents = [check for check in checks if check.name == "agent"]
    assert len(agents) == 1
    check = agents[0]
    assert "claude-code" in check.detail
    if version is None:
        assert check.level == "fail"
        assert "tidak ditemukan di PATH" in check.detail
        assert not calls.exists()
    else:
        assert check.level == ("ok" if version == "1.2.3" else "fail")
        found_path = check.detail.split("ditemukan di ", 1)[1].split(", versi ", 1)[0]
        assert Path(found_path) == binary
        assert f"versi {version}" in check.detail
        assert "login dicek saat run pertama" in check.detail
        assert calls.read_text(encoding="utf-8").splitlines() == ["--version"]
        if version == "0.9.9":
            assert "tidak cocok (butuh >= 1.0.0)" in check.detail
        else:
            assert ", cocok;" in check.detail
