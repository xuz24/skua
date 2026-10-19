"""Configuration, diagnostics, and interactive conversation entry points."""

from __future__ import annotations

import argparse
import json
import sys

from . import __version__
from .config import ConfigError, load_config, resolve_credential, resolve_root
from .setup import run_setup
from .squak import check_local, check_full
from .client import ModelError
from .terminal import run_chat


def _common(parser: argparse.ArgumentParser) -> None:
    # Suppressed defaults allow the same options before or after a subcommand.
    parser.add_argument("--root", default=argparse.SUPPRESS, help="project directory (default: launch directory)")
    parser.add_argument("--profile", default=argparse.SUPPRESS, help="named model profile")


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(prog="skua", description="A lightweight terminal coding harness (interactive conversation and diagnostics).")
    result.add_argument("--version", action="version", version=f"skua {__version__}")
    _common(result)
    commands = result.add_subparsers(dest="command")
    init = commands.add_parser("init", help="configure global defaults or project overrides; no network requests")
    _common(init)
    init.add_argument("--project", action="store_true", help="write <root>/.skua/config.toml")
    profiles = commands.add_parser("profiles", help="list configured profiles without loading keys")
    _common(profiles)
    squak = commands.add_parser("squak", help="check local prerequisites and endpoint/tool support (may consume tokens)")
    _common(squak)
    squak.add_argument("--local", action="store_true", help="run local checks without an endpoint request")
    config = commands.add_parser("config", help="inspect or validate configuration offline")
    _common(config)
    actions = config.add_subparsers(dest="config_command", required=True)
    show = actions.add_parser("show", help="show resolved non-credential configuration")
    _common(show)
    show.add_argument("--sources", action="store_true", help="include the source of each setting")
    check = actions.add_parser("check", help="validate configuration without contacting the endpoint")
    _common(check)
    check.add_argument("--credentials", action="store_true", help="also check the selected profile's credential is available (never print it)")
    return result


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        root = resolve_root(getattr(args, "root", None))
        selected = getattr(args, "profile", None)
        if args.command == "init":
            run_setup(root, project=args.project, profile=selected)
            return 0
        config = load_config(root, profile=selected)
        if args.command == "squak":
            return check_local(config) if args.local else check_full(config)
        if args.command == "profiles":
            if not config.profiles:
                print("No profiles configured. Run skua init.")
            for name in sorted(config.profiles):
                print(f"{'*' if name == config.selected_profile else ' '} {name}")
            return 0
        if args.command == "config":
            if args.config_command == "show":
                # Never read .env or resolve credentials for display.
                report = {
                    "root": str(root), "selected_profile": config.selected_profile,
                    "config": config.raw,
                    "resolved_paths": {
                        "environment": str(config.environment.path) if config.environment.path else None,
                        "notes": str(config.notes_directory), "sessions": str(config.sessions_directory),
                    },
                }
                if args.sources:
                    report["sources"] = dict(config.sources)
                print(json.dumps(report, indent=2))
            else:
                if args.credentials:
                    resolve_credential(config)
                print("Configuration is valid." + (" Selected-profile credentials are ready (or authentication is disabled)." if args.credentials else ""))
            return 0
        return run_chat(config)
    except (ConfigError, ModelError) as exc:
        print(f"skua: {exc}", file=sys.stderr)
        return 2
    except (KeyboardInterrupt, EOFError):
        print("\nCancelled; no pending configuration or conversation saved.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
