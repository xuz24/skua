"""Interactive, scoped configuration edits; no endpoint or shell execution."""

from __future__ import annotations

from copy import deepcopy
import os
from pathlib import Path
import stat
import tempfile
from typing import Any, Callable

import tomlkit

from .config import (
    ConfigError, PROFILE_NAME, build_config, global_config_path, parse_config,
    resolve_root, validate_layer,
)
from .environment import detect_shell, discover_environments

MISSING = object()


def _get(data: dict[str, Any], path: tuple[str, ...]) -> Any:
    current: Any = data
    for key in path:
        if not isinstance(current, dict) or key not in current:
            return MISSING
        current = current[key]
    return current


def _set(data: Any, path: tuple[str, ...], value: Any) -> None:
    current = data
    for key in path[:-1]:
        if key not in current:
            if value is MISSING:
                return
            current[key] = {}
        current = current[key]
    if value is MISSING:
        current.pop(path[-1], None)
    else:
        current[path[-1]] = value


def _snapshot(path: Path) -> bytes | None:
    try:
        return path.read_bytes()
    except FileNotFoundError:
        return None
    except OSError:
        raise ConfigError(f"{path}: could not read setup destination") from None


def write_config(path: Path, content: str, expected: bytes | None) -> None:
    """Replace only a reviewed snapshot; keep previous content on pre-replace failure."""
    path = path.expanduser().resolve()
    temporary: str | None = None
    try:
        if _snapshot(path) != expected:
            raise ConfigError(f"{path}: configuration changed during setup; rerun setup")
        path.parent.mkdir(parents=True, exist_ok=True)
        mode = stat.S_IMODE(path.stat().st_mode) if expected is not None else 0o600
        fd, temporary = tempfile.mkstemp(prefix=".config-", suffix=".toml.tmp", dir=path.parent)
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
            os.fchmod(stream.fileno(), mode)
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        if _snapshot(path) != expected:
            raise ConfigError(f"{path}: configuration changed during setup; rerun setup")
        os.replace(temporary, path)
        temporary = None
    except OSError:
        raise ConfigError(f"{path}: could not save configuration; previous file was not intentionally replaced") from None
    finally:
        if temporary is not None:
            Path(temporary).unlink(missing_ok=True)


def run_setup(root: Path, *, project: bool = False, profile: str | None = None,
              ask: Callable[[str], str] = input, emit: Callable[[str], None] = print) -> bool:
    """Return true only when a changed configuration has been saved."""
    root = resolve_root(root)
    global_path = global_config_path().resolve()
    destination = (root / ".skua" / "config.toml").resolve() if project else global_path
    original = _snapshot(destination)
    scoped = parse_config(original, str(destination)) if original is not None else {"schema_version": 1}
    global_snapshot = _snapshot(global_path) if project else None
    global_data = parse_config(global_snapshot, str(global_path)) if global_snapshot is not None else {}
    candidate = deepcopy(scoped)

    def effective():
        layers = [(str(global_path), global_data)] if project else []
        layers.append((str(destination), candidate))
        return build_config(root, layers, profile)

    initial = effective()
    name = profile or initial.default_profile
    while True:
        entered = ask(f"Profile name [{name}]: ").strip()
        name = entered or name
        if PROFILE_NAME.fullmatch(name):
            break
        emit("Use letters, digits, underscores or hyphens; start with a letter or digit (max 64).")

    emit(f"Setup scope: {'project' if project else 'global'} ({destination})")
    emit("Enter keeps the existing/inherited value. :inherit removes this scope's override.")
    emit("For booleans use yes/no; :disable is equivalent to no. No keys will be requested.")
    changes: dict[tuple[str, ...], Any] = {}
    # Display inherited defaults without materializing them into this scope.
    display = deepcopy(initial.raw)
    display["profiles"].setdefault(name, {
        "stream": True, "auth": {"enabled": True, "env_var": "SKUA_API_KEY"},
    })
    if not initial.profiles and "default_profile" not in scoped and name != "default":
        candidate["default_profile"] = name
        display["default_profile"] = name
        changes[("default_profile",)] = name

    def prompt(path: tuple[str, ...], label: str, *, boolean: bool = False,
               fallback: Any = MISSING, accept_fallback: bool = False) -> Any:
        current = _get(display, path)
        had_value = current is not MISSING
        if current is MISSING:
            current = fallback
        local = _get(candidate, path)
        shown = "not set" if current is MISSING else str(current).lower() if boolean else str(current)
        origin = "this scope" if local is not MISSING else "inherited/default"
        while True:
            response = ask(f"{label} [{shown}; {origin}]: ").strip()
            if not response:
                if not had_value and accept_fallback and current is not MISSING:
                    _set(candidate, path, current)
                    changes[path] = current
                return current
            if response == ":inherit":
                value = MISSING
            elif boolean and response.lower() in {"yes", "y", "true", "no", "n", "false", ":disable"}:
                value = response.lower() in {"yes", "y", "true"}
            elif boolean:
                emit("Enter yes, no, or :inherit.")
                continue
            else:
                value = response
            if value is not MISSING:
                probe = {"schema_version": 1}
                _set(probe, path, value)
                try:
                    validate_layer(probe, str(destination))
                except ConfigError as exc:
                    emit(str(exc))
                    continue
            _set(candidate, path, value)
            changes[path] = value
            return value

    p = ("profiles", name)
    prompt(p + ("base_url",), "API base URL (including API prefix, e.g. /v1)")
    prompt(p + ("model",), "Model identifier")
    prompt(p + ("stream",), "Stream responses", boolean=True)
    prompt(p + ("auth", "enabled"), "Use API-key authentication", boolean=True)
    prompt(p + ("auth", "env_var"), "API-key environment variable name")
    prompt(("default_profile",), "Default profile")
    prompt(("shell", "executable"), "Bash/Zsh executable (blank keeps auto-detection)", fallback=detect_shell())
    prompt(("shell", "startup", "enabled"), "Load interactive, non-login shell startup config", boolean=True)
    for candidate_env in discover_environments(root):
        emit(f"Detected {candidate_env.kind}: {candidate_env.path}")
    emit("Select inherited execution or enter a detected/manual environment path or Conda name below.")
    kind = prompt(("environment", "kind"), "Command environment: inherited / venv / conda")
    if kind is MISSING:
        # Removing the override reverts to the lower layer, not an empty kind.
        kind = global_data.get("environment", {}).get("kind", "inherited") if project else "inherited"
    if kind == "venv":
        prompt(("environment", "path"), "Virtualenv path (relative to project root)", fallback=".venv", accept_fallback=True)
        # A selected path replaces a prior local Conda name.
        if "path" in candidate.get("environment", {}) and "name" in candidate["environment"]:
            _set(candidate, ("environment", "name"), MISSING)
            changes[("environment", "name")] = MISSING
    elif kind == "conda":
        selector = ask("Conda selector: name or path [name]: ").strip() or "name"
        if selector not in {"name", "path"}:
            raise ConfigError("setup: Conda selector must be name or path")
        prompt(("environment", selector), f"Conda {selector}")
        other = "path" if selector == "name" else "name"
        if selector in candidate.get("environment", {}) and other in candidate["environment"]:
            _set(candidate, ("environment", other), MISSING)
            changes[("environment", other)] = MISSING
    prompt(("notes", "directory"), "Default notes directory")
    prompt(("sessions", "directory"), "Default session directory")
    prompt(("lint", "enabled"), "Enable a project lint command", boolean=True)
    # Always allow editing the dormant command without enabling lint.
    prompt(("lint", "command"), "Lint command (optional when disabled)")

    result = effective()
    # A new or selected profile must be useful, even if the chosen default differs.
    if name not in result.profiles:
        raise ConfigError("setup: enter a base URL and model for the profile")
    result.profile()
    document = tomlkit.parse(original.decode("utf-8")) if original is not None else tomlkit.document()
    document["schema_version"] = 1
    for path, value in changes.items():
        _set(document, path, value)
    rendered = tomlkit.dumps(document)
    # Round-trip through the authoritative parser before asking to save.
    parsed = parse_config(rendered.encode(), str(destination))
    if parsed != candidate:
        raise ConfigError("setup: generated configuration does not match the reviewed changes")
    if original is not None and rendered.encode() == original:
        emit("No configuration changes.")
        return False
    emit(f"Proposed changes to {destination}:")
    if original is None:
        emit("  schema_version = 1 (new file)")
    for path, value in changes.items():
        if _get(scoped, path) == value:
            continue
        # Shell command text may contain secrets. Never print it in the summary.
        if value is MISSING:
            summary = "inherit"
        elif path == ("lint", "command"):
            summary = "set (command omitted)"
        else:
            summary = repr(value)
        emit(f"  {'.'.join(path)}: {summary}")
    if ask("Save this configuration? [y/N]: ").strip().lower() not in {"yes", "y"}:
        emit("Setup cancelled; no configuration written.")
        return False
    if project and _snapshot(global_path) != global_snapshot:
        raise ConfigError("global configuration changed during setup; rerun setup")
    write_config(destination, rendered, original)
    emit(f"Saved {destination}")
    return True
