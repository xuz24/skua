"""Configuration layers, validation, and credential lookup (no network access)."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field
import os
from pathlib import Path
import re
import tomllib
from typing import Any, Mapping
from urllib.parse import urlsplit

from dotenv import dotenv_values


class ConfigError(ValueError):
    """A safe-to-display error that never includes a rejected value."""


PROFILE_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}\Z")
VARIABLE_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")

# Leaf strings name validation rules; only profiles has user-defined table keys.
PROFILE_SCHEMA = {
    "base_url": "url", "model": "text", "stream": "bool",
    "auth": {"enabled": "bool", "env_var": "variable"},
}
SCHEMA = {
    "schema_version": "version", "default_profile": "profile",
    "profiles": {"*": PROFILE_SCHEMA},
    "shell": {"executable": "shell", "startup": {"enabled": "bool"}},
    "environment": {"kind": "environment", "path": "path", "name": "text"},
    "commands": {"timeout_seconds": "positive", "dotenv_allowlist": "variables"},
    "output": {"model_max_bytes": "positive", "retain_max_bytes": "positive"},
    "agent": {"tool_calls_per_block": "positive"},
    "notes": {"directory": "path"}, "sessions": {"directory": "path"},
    "lint": {"enabled": "bool", "command": "text"},
    "sensitive": {"filenames": "filenames", "patterns": "patterns"},
}
DEFAULTS: dict[str, Any] = {
    "schema_version": 1, "default_profile": "default", "profiles": {},
    "shell": {"startup": {"enabled": False}},
    "environment": {"kind": "inherited"},
    "commands": {"timeout_seconds": 300, "dotenv_allowlist": []},
    "output": {"model_max_bytes": 20_000, "retain_max_bytes": 1_000_000},
    "agent": {"tool_calls_per_block": 20},
    "notes": {"directory": ".notes"}, "sessions": {"directory": ".skua/sessions"},
    "lint": {"enabled": False}, "sensitive": {"filenames": [], "patterns": []},
}
PROFILE_DEFAULTS = {"stream": True, "auth": {"enabled": True, "env_var": "SKUA_API_KEY"}}


@dataclass(frozen=True)
class Auth:
    enabled: bool = True
    env_var: str = "SKUA_API_KEY"


@dataclass(frozen=True)
class Profile:
    base_url: str
    model: str
    stream: bool
    auth: Auth


@dataclass(frozen=True)
class Shell:
    executable: Path | None
    startup_enabled: bool


@dataclass(frozen=True)
class Environment:
    kind: str
    path: Path | None
    name: str | None


@dataclass(frozen=True)
class Commands:
    timeout_seconds: int
    dotenv_allowlist: tuple[str, ...]


@dataclass(frozen=True)
class Output:
    model_max_bytes: int
    retain_max_bytes: int


@dataclass(frozen=True)
class Lint:
    enabled: bool
    command: str | None


@dataclass(frozen=True)
class Sensitive:
    filenames: tuple[str, ...]
    patterns: tuple[str, ...]


@dataclass(frozen=True)
class Config:
    root: Path
    default_profile: str
    selected_profile: str
    profiles: Mapping[str, Profile]
    shell: Shell
    environment: Environment
    commands: Commands
    output: Output
    tool_calls_per_block: int
    notes_directory: Path
    sessions_directory: Path
    lint: Lint
    sensitive: Sensitive
    # These contain configuration only, never resolved credential values.
    sources: Mapping[str, str] = field(repr=False)
    raw: Mapping[str, Any] = field(repr=False)

    def profile(self) -> Profile:
        if self.selected_profile not in self.profiles:
            raise ConfigError(
                "selected profile is not configured; run skua init or choose --profile"
            )
        return self.profiles[self.selected_profile]


def global_config_path() -> Path:
    return Path.home() / ".config" / "skua" / "config.toml"


def resolve_root(root: str | Path | None = None) -> Path:
    try:
        result = Path(root).expanduser().resolve() if root is not None else Path.cwd().resolve()
        if not result.is_dir():
            raise ConfigError("--root: expected an existing directory")
        return result
    except (OSError, RuntimeError, ValueError) as exc:
        if isinstance(exc, ConfigError):
            raise
        raise ConfigError("--root: could not resolve project directory") from None


def _fail(source: str, path: str, message: str) -> None:
    raise ConfigError(f"{source}: {path}: {message}")


def _is_text(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip()) and "\x00" not in value


def _valid_url(value: Any) -> bool:
    if not _is_text(value) or any(c.isspace() for c in value):
        return False
    try:
        url = urlsplit(value)
        return (url.scheme in {"http", "https"} and bool(url.hostname)
                and url.username is None and url.password is None
                and not url.query and not url.fragment and "?" not in value
                and "#" not in value and (url.port is None or url.port > 0))
    except ValueError:
        return False


def _valid_pattern(value: Any) -> bool:
    return (_is_text(value) and not value.startswith(("/", "!", "~"))
            and "\\" not in value and ".." not in value.split("/")
            and all("**" not in part or part == "**" for part in value.split("/")))


def _validate_leaf(value: Any, rule: str, source: str, path: str) -> None:
    valid = False
    expectation = rule
    if rule == "bool":
        valid, expectation = type(value) is bool, "expected a boolean"
    elif rule == "version":
        valid, expectation = type(value) is int and value == 1, "expected schema_version = 1"
    elif rule == "positive":
        valid, expectation = type(value) is int and value > 0, "expected a positive integer"
    elif rule in {"text", "path"}:
        valid, expectation = _is_text(value), "expected a nonempty string without NUL"
    elif rule in {"variable", "profile"}:
        regex = VARIABLE_NAME if rule == "variable" else PROFILE_NAME
        valid = isinstance(value, str) and regex.fullmatch(value) is not None
        expectation = f"expected a valid {rule} name"
    elif rule == "url":
        valid = _valid_url(value)
        expectation = "expected an HTTP(S) API prefix without credentials, query, or fragment"
    elif rule == "shell":
        valid = (_is_text(value) and Path(value).is_absolute()
                 and Path(value).name in {"bash", "zsh"})
        expectation = "expected an absolute Bash or Zsh executable path"
    elif rule == "environment":
        valid = isinstance(value, str) and value in {"inherited", "venv", "conda"}
        expectation = "expected inherited, venv, or conda"
    elif rule in {"variables", "filenames", "patterns"}:
        valid = isinstance(value, list)
        if valid:
            for item in value:
                if rule == "variables":
                    ok = isinstance(item, str) and VARIABLE_NAME.fullmatch(item) is not None
                elif rule == "filenames":
                    ok = (_is_text(item) and item not in {".", ".."}
                          and not any(c in item for c in "/\\*?[]"))
                else:
                    ok = _valid_pattern(item)
                if not ok:
                    valid = False
                    break
        expectation = f"expected an array of valid {rule}"
    if not valid:
        _fail(source, path, expectation)


def validate_layer(data: dict[str, Any], source: str) -> None:
    def visit(table: dict[str, Any], schema: dict[str, Any], prefix: str = "") -> None:
        for key, value in table.items():
            path = f"{prefix}.{key}" if prefix else key
            if "*" in schema:
                _validate_leaf(key, "profile", source, path)
                spec = schema["*"]
            elif key not in schema:
                _fail(source, path, "unknown setting")
            else:
                spec = schema[key]
            if isinstance(spec, dict):
                if not isinstance(value, dict):
                    _fail(source, path, "expected a table")
                visit(value, spec, path)
            else:
                _validate_leaf(value, spec, source, path)

    visit(data, SCHEMA)
    if "schema_version" not in data:
        _fail(source, "schema_version", "required; add schema_version = 1")
    env = data.get("environment", {})
    if "path" in env and "name" in env:
        _fail(source, "environment", "specify only one of path or name in this scope")


def parse_config(content: bytes, source: str) -> dict[str, Any]:
    try:
        data = tomllib.loads(content.decode("utf-8"))
    except (UnicodeDecodeError, tomllib.TOMLDecodeError):
        # Parser errors can contain source values; do not forward them.
        raise ConfigError(f"{source}: invalid UTF-8 TOML") from None
    validate_layer(data, source)
    return data


def read_config(path: Path) -> dict[str, Any]:
    try:
        content = path.read_bytes()
    except FileNotFoundError:
        return {}
    except OSError:
        raise ConfigError(f"{path}: could not read configuration") from None
    return parse_config(content, str(path))


def merge_layers(layers: list[tuple[str, dict[str, Any]]]) -> tuple[dict[str, Any], dict[str, str]]:
    merged: dict[str, Any] = {}
    sources: dict[str, str] = {}

    def merge(target: dict[str, Any], patch: dict[str, Any], source: str, prefix: str = "") -> None:
        for key, value in patch.items():
            path = f"{prefix}.{key}" if prefix else key
            sources[path] = source
            if isinstance(value, dict):
                merge(target.setdefault(key, {}), value, source, path)
            else:
                target[key] = deepcopy(value)

    merge(merged, DEFAULTS, "built-in defaults")
    for source, data in layers:
        if not data:
            continue
        validate_layer(data, source)
        env = data.get("environment", {})
        # Name and path are alternate selectors, not two independently active fields.
        for selected, other in (("path", "name"), ("name", "path")):
            if selected in env:
                merged["environment"].pop(other, None)
                sources.pop(f"environment.{other}", None)
        merge(merged, data, source)
    for name, profile in merged["profiles"].items():
        base = deepcopy(PROFILE_DEFAULTS)
        # Set profile defaults without claiming they came from a user config.
        for key in ("stream",):
            if key not in profile:
                profile[key] = base[key]
                sources[f"profiles.{name}.{key}"] = "built-in defaults"
        auth = profile.setdefault("auth", {})
        for key, value in base["auth"].items():
            if key not in auth:
                auth[key] = value
                sources[f"profiles.{name}.auth.{key}"] = "built-in defaults"
    return merged, sources


def build_config(root: Path, layers: list[tuple[str, dict[str, Any]]],
                 profile: str | None = None) -> Config:
    raw, sources = merge_layers(layers)

    def fail(path: str, message: str) -> None:
        lookup = path
        while lookup not in sources and "." in lookup:
            lookup = lookup.rsplit(".", 1)[0]
        _fail(sources.get(lookup, "configuration"), path, message)

    profiles = {}
    for name, entry in raw["profiles"].items():
        for required in ("base_url", "model"):
            if required not in entry:
                fail(f"profiles.{name}.{required}", "required in the merged profile")
        entry["base_url"] = entry["base_url"].rstrip("/")
        profiles[name] = Profile(entry["base_url"], entry["model"], entry["stream"], Auth(**entry["auth"]))
    if profile is not None:
        _validate_leaf(profile, "profile", "CLI", "profile")
    selected = profile if profile is not None else raw["default_profile"]
    if raw["output"]["retain_max_bytes"] < raw["output"]["model_max_bytes"]:
        fail("output.retain_max_bytes", "must be at least output.model_max_bytes")
    if raw["lint"]["enabled"] and "command" not in raw["lint"]:
        fail("lint.command", "required when lint is enabled")
    env = raw["environment"]
    if env["kind"] == "venv" and "path" not in env:
        fail("environment.path", "required for venv")
    if env["kind"] == "conda" and not any(k in env for k in ("path", "name")):
        fail("environment", "Conda requires a name or path")

    def resolve(value: str | None) -> Path | None:
        if value is None:
            return None
        try:
            path = Path(value).expanduser()
            return (path if path.is_absolute() else root / path).resolve()
        except (OSError, RuntimeError, ValueError):
            raise ConfigError("configuration: could not resolve configured path") from None

    return Config(
        root=root, default_profile=raw["default_profile"], selected_profile=selected,
        profiles=profiles,
        shell=Shell(resolve(raw["shell"].get("executable")), raw["shell"]["startup"]["enabled"]),
        environment=Environment(env["kind"], resolve(env.get("path")), env.get("name")),
        commands=Commands(raw["commands"]["timeout_seconds"], tuple(raw["commands"]["dotenv_allowlist"])),
        output=Output(**raw["output"]), tool_calls_per_block=raw["agent"]["tool_calls_per_block"],
        notes_directory=resolve(raw["notes"]["directory"]),
        sessions_directory=resolve(raw["sessions"]["directory"]),
        lint=Lint(raw["lint"]["enabled"], raw["lint"].get("command")),
        sensitive=Sensitive(tuple(raw["sensitive"]["filenames"]), tuple(raw["sensitive"]["patterns"])),
        sources=sources, raw=raw,
    )


def load_config(root: str | Path | None = None, *, profile: str | None = None,
                global_path: Path | None = None, project: bool = True) -> Config:
    resolved = resolve_root(root)
    global_path = global_path if global_path is not None else global_config_path()
    layers = [(str(global_path), read_config(global_path))]
    if project:
        path = resolved / ".skua" / "config.toml"
        layers.append((str(path), read_config(path)))
    return build_config(resolved, layers, profile)


def read_dotenv(root: Path) -> dict[str, str]:
    path = root / ".env"
    try:
        # Explicit path and no interpolation: never search ancestors or mutate environ.
        if not path.exists():
            return {}
        with path.open(encoding="utf-8") as stream:
            values = dotenv_values(stream=stream, interpolate=False)
    except (OSError, UnicodeError):
        raise ConfigError(f"{path}: could not read UTF-8 environment file") from None
    return {key: value for key, value in values.items() if value is not None}


def resolve_credential(config: Config, environ: Mapping[str, str] | None = None) -> str | None:
    auth = config.profile().auth
    if not auth.enabled:
        return None
    inherited = os.environ if environ is None else environ
    # Presence wins even for an empty process value; don't silently fall back.
    value = inherited.get(auth.env_var) if auth.env_var in inherited else read_dotenv(config.root).get(auth.env_var)
    if not value or not value.strip():
        raise ConfigError(f"credential variable {auth.env_var} is missing or empty; set it in the environment or project .env")
    return value


def command_environment(config: Config, environ: Mapping[str, str] | None = None) -> dict[str, str]:
    result = dict(os.environ if environ is None else environ)
    if config.commands.dotenv_allowlist:
        values = read_dotenv(config.root)
        for name in config.commands.dotenv_allowlist:
            if name not in result and name in values:
                result[name] = values[name]
    return result
