# Skua

A lightweight, seagull-inspired terminal coding harness for Python 3.12+ on
macOS/Linux, with Bash or Zsh project commands.

**Current status:** milestones 1–3 are implemented: setup/configuration, internal
shell execution, streaming terminal conversation, and local/endpoint diagnostics.
Conversation history is in memory only. Saving/resume and model-visible tools
remain planned; chat cannot read, edit, or execute project files.

## Install from this checkout

```sh
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
python -m pip install --no-deps .
skua --help
```

Use `python -m skua` as an alternative to the `skua` entry point. Installation
requires package downloads; setup and configuration commands make no network
requests. Skua's own Python environment is independent of the environment used by
your project, which may be written in any language.

## Setup

```sh
skua init
skua init --project
skua init --profile local
skua --root /path/to/project init --project
```

Global defaults live in `~/.config/skua/config.toml`; project overrides live in
`<root>/.skua/config.toml`. The root is the launch directory, or `--root`, not an
automatically discovered Git root. `--root` and `--profile` also work after the
subcommand.

Setup asks for a profile, API base URL, model, authentication-variable name, shell,
startup preference, environment selector, and output directories. Enter keeps
existing or inherited values. `:inherit` removes a field's override in the current
scope; booleans accept yes/no and `:disable`. Missing required endpoint/model values
must be entered. Setup shows its destination and proposed changes before writing.
Declining or cancelling leaves files unchanged. Rerunning setup preserves comments
and settings you do not change.

Startup loading defaults off. Setup lists project `.venv`/`venv`, active virtualenv
and Conda paths, and registered Conda paths from `~/.conda/environments.txt`.
Manual paths and Conda names are supported. Relative environment paths resolve
against the project root even when set globally. Setup creates no environment
and installs no dependencies.

A minimal local profile:

```toml
schema_version = 1
default_profile = "default"

[profiles.default]
base_url = "http://localhost:8000/v1"
model = "your-model-id"

[profiles.default.auth]
enabled = false
```

Use the API prefix, such as `/v1`, rather than a `/chat/completions` URL. Enter the
model identifier supported by your endpoint. `skua squak` checks compatibility;
setup does not guess models or test the endpoint.

For a hosted endpoint, enable authentication and name the environment variable:

```toml
[profiles.default.auth]
enabled = true
env_var = "SKUA_API_KEY"
```

Set that variable in the launch environment or in the project's `.env` file.
Setup never asks for the key itself. Use `.env.example` as a placeholder guide;
do not put secrets in TOML or commit `.env`.

Existing process variables take precedence, including empty values (an empty key
reports an error rather than falling back). Only the selected project's `.env`
is read, with `${NAME}` expansion disabled. Reading it does not change the process
environment. Credential lookup is separate from command-variable allowlisting.

## Inspect configuration

```sh
skua profiles
skua config show
skua config show --sources
skua config check
skua --profile local config check --credentials
```

`show` displays merged configuration, resolved paths, and optionally field sources;
it never reads `.env` or resolves keys. `check` validates syntax and merged settings.
`--credentials` additionally checks the selected profile's key is present, without
printing it or contacting the endpoint. A configuration-only check can succeed
before a model profile has been set up. Plain `skua` starts interactive chat using
the selected profile and requires an interactive terminal.

Unknown settings and invalid types fail with file/field errors. Each config file
requires `schema_version = 1`. Explicit CLI selection overrides project and global
selection. Nested tables merge field by field; arrays replace inherited arrays,
and an empty array clears a user list. Optional features use `enabled = false` to
disable without falling back. User sensitive-file lists cannot disable built-ins.

For example, a project can change just the model and disable inherited linting:

```toml
schema_version = 1

[profiles.default]
model = "project-model"

[lint]
enabled = false
```

See the [configuration schema](.docs/config-schema.md) for all fields. Configuration
accepts future milestone settings, but those settings do not enable tools that
have not yet been implemented. File access approval and sensitive matching belong
to the later tool milestone.

## Conversation and endpoint diagnostics

```sh
skua squak                 # Local checks plus a synthetic endpoint/tool test
skua                       # Interactive chat with the default profile
skua --profile local       # Chat with a named profile
```

Full diagnostics and chat startup announce a small synthetic request that may
consume tokens. Native function-tool support is required: a missing/invalid
response stops startup rather than silently downgrading compatibility. The test
uses only a fixed diagnostic prompt, never project contents, and executes no
returned calls. It checks the profile's configured streaming or nonstreaming mode.

Enter sends; Alt+Enter inserts a newline. Bracketed multiline paste remains in the
editable input buffer until submitted. Ctrl-C cancels an active response and
returns to the prompt, or clears an unsent draft. With an empty draft, two
consecutive Ctrl-C presses request exit; Ctrl-D on empty input does the same.
`/help`, `/new`, and `/exit` are available. New/exit with history requires Discard
or Cancel; saving is explicitly unavailable until milestone 4. Nothing writes
transcripts, notes, or terminal history files automatically.

Text streams as plain text; partial replies remain marked incomplete in memory
after cancellation or failure. The HTTP client reuses connections, uses a 10-second
connect timeout, 60-second I/O timeout, and 300-second total deadline per attempt.
Temporary transport failures and HTTP 408/429/500/502/503/504 retry up to three times
with 1/2/4-second delays, cancellable with Ctrl-C. Once text or tool arguments
arrive, transport failures end the turn without replaying the partial response.
Authentication/configuration and malformed-response errors fail immediately.
Responses are limited to 8 MB and individual streamed events to 1 MB.

Protocol and terminal tests use controlled mock responses. A live U-M Toolkit
check also verified streaming chat and native-tool diagnostics after fixing support
for Azure prompt-filter metadata events. Configure an endpoint and run `skua squak`
for a compatibility check with your selected model. Chat sends user-entered conversation text;
project-file discovery and model tools await milestone 5.

## Local diagnostics and command execution

```sh
skua squak --local
skua --root /path/to/project squak --local
```

Local diagnostics validates the configured shell/environment using a fixed no-op
command. It requires no model profile or API key and makes no endpoint request.
It runs configured startup and activation scripts, which can have their own side
effects. Failures return a nonzero exit code. Omit `--local` to additionally run
the synthetic endpoint test described above.

Internal command execution starts a fresh Bash/Zsh process in the selected root.
Startup defaults off: Bash uses `--noprofile --norc` and removes `BASH_ENV`/`ENV`;
Zsh uses `-f` (system `/etc/zshenv` still runs). Opt-in uses interactive, non-login
startup; startup output is captured, and job-control warnings are possible because
commands have no terminal or interactive stdin. Virtualenv activation or Conda's
shell hook and activation runs afterward. Conda must be available via `CONDA_EXE`
or PATH. Missing environments or failed activation stop execution. Working
directory and exports do not carry between invocations.

Output capture drains stdout/stderr concurrently, retaining beginning/end bytes.
The total 1,000,000-byte retention budget and 20,000-byte preview budget each split
equally between streams; the odd byte goes to stdout. Labels and omission markers
are additional display text. Reports distinguish discarded bytes from retained
output omitted by a preview. Capture stays in bounded memory and writes no temp
files. Invalid or split UTF-8 is displayed with replacement characters.

Commands default to a 300-second timeout. Cancellation/timeout sends SIGTERM to
the process group, allows up to five seconds, then SIGKILLs remaining members.
Captured output and file changes remain; detached processes are outside this
guarantee. This is not a filesystem sandbox. The runner is an internal primitive
for later approved tools; this milestone adds no general command-execution CLI.

## Development

```sh
python -m pip install -r requirements-dev.txt
python -m pip install --no-build-isolation --no-deps -e .
python -m pytest
```

Regenerate pins from `pyproject.toml`, runtime first:

```sh
python -m piptools compile --no-build-isolation --strip-extras --output-file=requirements.txt pyproject.toml
python -m piptools compile --no-build-isolation --strip-extras --allow-unsafe --extra=dev --constraint=requirements.txt --output-file=requirements-dev.txt pyproject.toml
```

Pins are generated with Python 3.12. Platform/Python coverage is recorded in the
implementation plan; they are not yet validated across the full macOS/Linux
matrix. Tests use temporary homes/projects and do not need an endpoint or real key.

Design: [implementation plan](.docs/implementation-plan.md),
[session schema](.docs/session-schema.md), and [product decisions](.docs/design.md).
