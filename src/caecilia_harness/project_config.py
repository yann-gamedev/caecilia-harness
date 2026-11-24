"""Per-project settings in `.caecilia.toml` and how they combine with command-line options.

Found by walking up from the working folder to the root of its Git repo (ADR-0015). Priority for
every value: the option given on the command line, then the file, then the default. The file never
holds an API key: those live in the OS keychain (`caecilia key set`).
"""

from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import subprocess
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

CONFIG_NAME = ".caecilia.toml"
SUPPORTED_PROVIDERS = ("deepseek",)
WorkerName = Literal["fake", "native"]

# Field names that look like they could carry a credential.
_SECRET_FIELD = re.compile(r"key|token|secret|password|passwd|credential", re.IGNORECASE)
_USE_KEY_SET = "API key disimpan dengan `caecilia key set`, bukan di file ini"


class ConfigError(ValueError):
    """`.caecilia.toml` cannot be used; `problems` lists every reason."""

    def __init__(self, problems: list[str], source: str = CONFIG_NAME) -> None:
        self.problems = problems
        self.source = source
        super().__init__(f"{source}: " + "; ".join(problems))


class NotAGitRepoError(ValueError):
    """The folder is not inside a Git repository."""


class MissingOptionError(ValueError):
    """A required value is neither on the command line nor in `.caecilia.toml`."""

    def __init__(self, option: str, field: str, why: str = "") -> None:
        detail = f" ({why})" if why else ""
        super().__init__(
            f"{option} wajib diisi{detail}: berikan {option} di perintah, atau isi `{field}` di "
            f"{CONFIG_NAME} (buat dengan `caecilia init`)"
        )


def parse_check(cmd: str, posix: bool | None = None) -> list[str]:
    """Split a check command into argv.

    Windows uses non-POSIX rules so backslashes in paths survive; quotes wrapping a whole token
    are then removed, since non-POSIX mode keeps them.
    """
    posix = os.name != "nt" if posix is None else posix
    tokens = shlex.split(cmd, posix=posix)
    if not posix:
        tokens = [t[1:-1] if len(t) >= 2 and t[0] == t[-1] and t[0] in "\"'" else t for t in tokens]
    return tokens


class ProjectConfig(BaseModel):
    """The `.caecilia.toml` schema (docs/architecture.md). Unknown fields are refused."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    version: Literal[1] = 1
    check: str | list[str] | None = None
    worker: WorkerName | None = None
    provider: str | None = None
    model: str | None = None
    max_parallel: int | None = Field(default=None, ge=1)
    provider_slots: int | None = Field(default=None, ge=1)
    exec_slots: int | None = Field(default=None, ge=1)
    target: str | None = None
    #: argv prefixes approved automatically in non-interactive mode (CLI-02).
    allow_commands: list[list[str]] = Field(default_factory=list)

    @model_validator(mode="before")
    @classmethod
    def _no_secrets(cls, data: Any) -> Any:
        if isinstance(data, dict):
            secret = [name for name in data if _SECRET_FIELD.search(str(name))]
            if secret:
                raise ValueError(f"field {', '.join(secret)} ditolak: {_USE_KEY_SET}")
        return data

    @model_validator(mode="after")
    def _non_empty_lists(self) -> ProjectConfig:
        if isinstance(self.check, list) and not self.check:
            raise ValueError("check tidak boleh berupa list kosong")
        if any(not prefix for prefix in self.allow_commands):
            raise ValueError("setiap isi allow_commands harus berupa argv yang tidak kosong")
        return self


def repo_root(start: Path) -> Path:
    """The top of the Git repository containing `start`."""
    git = shutil.which("git")
    if git is None:
        raise NotAGitRepoError("git tidak ditemukan di PATH")
    done = subprocess.run(
        [git, "rev-parse", "--show-toplevel"], cwd=start, capture_output=True, text=True
    )
    if done.returncode != 0:
        raise NotAGitRepoError(
            f"bukan repo Git: {start}; jalankan caecilia di dalam repo, atau pakai --repo"
        )
    return Path(done.stdout.strip()).resolve()


def find_config(start: Path, root: Path) -> Path | None:
    """`.caecilia.toml` in `start` or one of its parents, up to and including `root`."""
    folder, root = start.resolve(), root.resolve()
    while True:
        candidate = folder / CONFIG_NAME
        if candidate.is_file():
            return candidate
        if folder == root or folder.parent == folder:
            return None
        folder = folder.parent


def _problems(exc: ValidationError) -> list[str]:
    problems = []
    for error in exc.errors():
        location = ".".join(str(part) for part in error["loc"])
        message = str(error["msg"]).removeprefix("Value error, ")
        problems.append(f"{location}: {message}" if location else message)
    return problems


def parse_config(text: str, source: str = CONFIG_NAME) -> ProjectConfig:
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError([f"TOML tidak valid: {exc}"], source) from exc
    try:
        return ProjectConfig.model_validate(data)
    except ValidationError as exc:
        raise ConfigError(_problems(exc), source) from exc


def load_config(path: Path) -> ProjectConfig:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ConfigError([f"tidak bisa membaca file: {exc.strerror or exc}"], str(path)) from exc
    return parse_config(text, str(path))


def dump_config(config: ProjectConfig) -> str:
    """TOML for `config`, leaving out unset fields (used by `caecilia init`)."""
    lines = ["version = 1"]
    for name, value in config.model_dump(exclude={"version"}).items():
        if value is None or value == []:
            continue
        lines.append(f"{name} = {_toml_value(value)}")
    return "\n".join(lines) + "\n"


def _toml_value(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, str):
        # A JSON string is a valid TOML basic string (escapes for quotes, backslashes, controls).
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, list):
        return "[" + ", ".join(_toml_value(item) for item in value) + "]"
    raise TypeError(f"cannot write {type(value).__name__} to {CONFIG_NAME}")


@dataclass(frozen=True)
class CliValues:
    """What was given on the command line; None = not given."""

    check: str | None = None
    worker: str | None = None
    provider: str | None = None
    model: str | None = None
    max_parallel: int | None = None
    provider_slots: int | None = None
    exec_slots: int | None = None
    target: str | None = None


@dataclass(frozen=True)
class RunOptions:
    """The values a run actually uses, after CLI -> config -> default."""

    check: list[str] | None
    worker: WorkerName
    provider: str
    model: str | None
    max_parallel: int | None
    provider_slots: int
    exec_slots: int
    target: str | None
    allow_commands: list[list[str]] = field(default_factory=list)

    def require_check(self) -> list[str]:
        if not self.check:
            raise MissingOptionError("--check", "check")
        return self.check


def resolve_options(cli: CliValues, config: ProjectConfig | None) -> RunOptions:
    """Combine options: the command line wins, then `.caecilia.toml`, then the defaults."""
    cfg = config or ProjectConfig()

    def pick(cli_value: Any, cfg_value: Any, default: Any) -> Any:
        if cli_value is not None:
            return cli_value
        return cfg_value if cfg_value is not None else default

    raw_check = pick(cli.check, cfg.check, None)
    check = parse_check(raw_check) if isinstance(raw_check, str) else raw_check
    worker = pick(cli.worker, cfg.worker, "native")
    if worker not in ("fake", "native"):
        raise ValueError(f"worker tidak dikenal: {worker!r} (tersedia: fake, native)")
    provider = pick(cli.provider, cfg.provider, SUPPORTED_PROVIDERS[0])
    if worker == "native" and provider not in SUPPORTED_PROVIDERS:
        raise ValueError(
            f"provider {provider!r} belum didukung (tersedia: {', '.join(SUPPORTED_PROVIDERS)})"
        )
    return RunOptions(
        check=list(check) if check else None,
        worker=worker,
        provider=provider,
        model=pick(cli.model, cfg.model, None),
        max_parallel=pick(cli.max_parallel, cfg.max_parallel, None),
        provider_slots=pick(cli.provider_slots, cfg.provider_slots, 2),
        exec_slots=pick(cli.exec_slots, cfg.exec_slots, 2),
        target=pick(cli.target, cfg.target, None),
        allow_commands=[list(prefix) for prefix in cfg.allow_commands],
    )


OptionSource = Literal["cli", "config", "default"]


def option_sources(cli: CliValues, config: ProjectConfig | None) -> dict[str, OptionSource]:
    """Where each resolved option came from (shown by `/config` in the interactive session)."""
    cfg = config or ProjectConfig()
    sources: dict[str, OptionSource] = {}
    for name in (
        "check",
        "worker",
        "provider",
        "model",
        "max_parallel",
        "provider_slots",
        "exec_slots",
        "target",
    ):
        if getattr(cli, name) is not None:
            sources[name] = "cli"
        elif getattr(cfg, name) is not None:
            sources[name] = "config"
        else:
            sources[name] = "default"
    sources["allow_commands"] = "config" if cfg.allow_commands else "default"
    return sources


class ConfigExistsError(ValueError):
    """`.caecilia.toml` is already there and overwriting was not asked for."""


def write_config(root: Path, config: ProjectConfig, force: bool = False) -> Path:
    """Write `config` to `<root>/.caecilia.toml`; refuse to overwrite unless `force`.

    The text is parsed back before it is written, so the file on disk always loads.
    """
    path = root / CONFIG_NAME
    if path.exists() and not force:
        raise ConfigExistsError(f"{path} sudah ada (pakai --force untuk menimpa)")
    text = dump_config(config)
    parse_config(text, str(path))
    path.write_text(text, encoding="utf-8", newline="\n")
    return path


def stage_config(root: Path) -> None:
    """`git add .caecilia.toml` (only when the person asks; never commits)."""
    git = shutil.which("git")
    if git is None:
        raise NotAGitRepoError("git tidak ditemukan di PATH")
    subprocess.run([git, "add", "--", CONFIG_NAME], cwd=root, check=True, capture_output=True)


def project_settings(cwd: Path, repo: Path | None = None) -> tuple[Path, ProjectConfig | None]:
    """The repo to work on (`repo`, else the Git root of `cwd`) and its config, if any."""
    root = repo_root(repo if repo is not None else cwd)
    start = cwd.resolve() if repo is None else root
    path = find_config(start, root)
    return root, (load_config(path) if path is not None else None)
