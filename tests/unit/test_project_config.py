"""`.caecilia.toml`: schema, search, and option priority (backlog CLI-01, ADR-0015)."""

from __future__ import annotations

from pathlib import Path

import pytest

from caecilia_harness.project_config import (
    CONFIG_NAME,
    CliValues,
    ConfigError,
    MissingOptionError,
    NotAGitRepoError,
    ProjectConfig,
    dump_config,
    find_config,
    parse_config,
    project_settings,
    repo_root,
    resolve_options,
)
from tests.integration.conftest import git_repo  # noqa: F401 - fixture

EXAMPLE = """
version = 1
check = "python -m pytest"
worker = "native"
provider = "deepseek"
model = "some-model"
max_parallel = 2
target = "main"
allow_commands = [["python", "-m", "pytest"], ["uv", "run", "pytest"]]
"""


def test_example_from_the_architecture_doc_is_valid() -> None:
    config = parse_config(EXAMPLE)
    assert config.check == "python -m pytest"
    assert config.allow_commands == [["python", "-m", "pytest"], ["uv", "run", "pytest"]]


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ('version = 1\ncolour = "red"\n', "colour"),
        ("version = 2\n", "version"),
        ('version = 1\nworker = "robot"\n', "worker"),
        ("version = 1\nmax_parallel = 0\n", "max_parallel"),
        ("version = 1\ncheck = []\n", "list kosong"),
        ("version = 1\nallow_commands = [[]]\n", "allow_commands"),
        ('version = 1\ncheck = "x"\n[budget\n', "line 3"),
    ],
)
def test_invalid_configs_are_refused(text: str, expected: str) -> None:
    with pytest.raises(ConfigError) as info:
        parse_config(text)
    assert expected in str(info.value)


@pytest.mark.parametrize("name", ["api_key", "deepseek_key", "token", "GEMINI_API_KEY", "secret"])
def test_key_like_fields_are_refused_with_a_pointer_to_key_set(name: str) -> None:
    with pytest.raises(ConfigError) as info:
        parse_config(f'version = 1\n{name} = "sk-123"\n')
    assert "caecilia key set" in str(info.value)
    assert "sk-123" not in str(info.value)


# --- priority: CLI -> config -> default -------------------------------------------------------

CONFIG = ProjectConfig(
    check="python -m pytest",
    worker="fake",
    model="cfg-model",
    max_parallel=3,
    provider_slots=4,
    exec_slots=5,
    target="develop",
    allow_commands=[["python", "-m", "pytest"]],
)


@pytest.mark.parametrize(
    ("cli", "config", "field", "expected"),
    [
        (CliValues(), None, "worker", "native"),
        (CliValues(), CONFIG, "worker", "fake"),
        (CliValues(worker="native"), CONFIG, "worker", "native"),
        (CliValues(), None, "check", None),
        (CliValues(), CONFIG, "check", ["python", "-m", "pytest"]),
        (CliValues(check="uv run pytest -q"), CONFIG, "check", ["uv", "run", "pytest", "-q"]),
        (CliValues(), ProjectConfig(check=["py", "-m", "x"]), "check", ["py", "-m", "x"]),
        (CliValues(), None, "model", None),
        (CliValues(), CONFIG, "model", "cfg-model"),
        (CliValues(model="cli-model"), CONFIG, "model", "cli-model"),
        (CliValues(), None, "max_parallel", None),  # the plan or D11 decides later
        (CliValues(), CONFIG, "max_parallel", 3),
        (CliValues(max_parallel=1), CONFIG, "max_parallel", 1),
        (CliValues(), None, "provider_slots", 2),
        (CliValues(), CONFIG, "provider_slots", 4),
        (CliValues(provider_slots=1), CONFIG, "provider_slots", 1),
        (CliValues(), None, "exec_slots", 2),
        (CliValues(), CONFIG, "exec_slots", 5),
        (CliValues(), None, "target", None),
        (CliValues(), CONFIG, "target", "develop"),
        (CliValues(target="main"), CONFIG, "target", "main"),
        (CliValues(), None, "provider", "deepseek"),
        (CliValues(), CONFIG, "allow_commands", [["python", "-m", "pytest"]]),
    ],
)
def test_priority(
    cli: CliValues, config: ProjectConfig | None, field: str, expected: object
) -> None:
    assert getattr(resolve_options(cli, config), field) == expected


def test_missing_check_names_the_option_and_the_field() -> None:
    with pytest.raises(MissingOptionError) as info:
        resolve_options(CliValues(), None).require_check()
    message = str(info.value)
    assert "--check" in message and "`check`" in message and CONFIG_NAME in message


def test_unsupported_provider_for_the_native_worker() -> None:
    with pytest.raises(ValueError, match="belum didukung"):
        resolve_options(CliValues(), ProjectConfig(worker="native", provider="gemini"))
    # The fake worker never calls a provider, so the field does not matter there.
    assert (
        resolve_options(CliValues(worker="fake"), ProjectConfig(provider="gemini")).worker == "fake"
    )


def test_dump_then_parse_round_trips() -> None:
    config = ProjectConfig(
        check='python -c "print(\\"hi\\")"',
        worker="native",
        model="model/with:odd chars",
        exec_slots=1,
        allow_commands=[["C:\\Python\\python.exe", "-m", "pytest"]],
    )
    text = dump_config(config)
    assert "provider" not in text  # unset fields are left out
    assert parse_config(text) == config


# --- finding the repo and the file -----------------------------------------------------------


def test_config_is_found_from_a_subfolder(git_repo: Path) -> None:  # noqa: F811
    (git_repo / CONFIG_NAME).write_text('version = 1\nworker = "fake"\n', encoding="utf-8")
    deep = git_repo / "src" / "pkg"
    deep.mkdir(parents=True)

    root, config = project_settings(deep)

    assert root == git_repo.resolve()
    assert config is not None and config.worker == "fake"
    assert find_config(deep, root) == git_repo.resolve() / CONFIG_NAME


def test_nearest_config_wins_but_search_stops_at_the_repo_root(
    git_repo: Path,  # noqa: F811
    tmp_path: Path,
) -> None:
    (tmp_path / CONFIG_NAME).write_text('version = 1\nworker = "native"\n', encoding="utf-8")
    assert find_config(git_repo, git_repo) is None  # the file above the repo is ignored
    sub = git_repo / "sub"
    sub.mkdir()
    (sub / CONFIG_NAME).write_text('version = 1\nworker = "fake"\n', encoding="utf-8")
    assert find_config(sub, git_repo) == sub.resolve() / CONFIG_NAME


def test_no_config_is_fine(git_repo: Path) -> None:  # noqa: F811
    root, config = project_settings(git_repo)
    assert root == git_repo.resolve() and config is None


def test_outside_a_git_repo_is_a_clear_error(tmp_path: Path) -> None:
    with pytest.raises(NotAGitRepoError, match="bukan bagian dari repo Git"):
        repo_root(tmp_path)


def test_invalid_config_file_reports_its_path(git_repo: Path) -> None:  # noqa: F811
    (git_repo / CONFIG_NAME).write_text("version = 1\nnope = 1\n", encoding="utf-8")
    with pytest.raises(ConfigError) as info:
        project_settings(git_repo)
    assert CONFIG_NAME in info.value.source and "nope" in str(info.value)
