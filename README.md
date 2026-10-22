# Skua

A lightweight, seagull-inspired terminal coding harness for Python 3.12+ on
macOS/Linux, with Bash or Zsh project commands.

**Current status:** milestones 1–6 are implemented: setup/configuration, internal
shell execution, streaming terminal conversation, and local/endpoint diagnostics.
Conversation history stays in memory until explicitly saved. JSONL save/resume,
Markdown export, session locks, project instructions, read/search tools, and approved
shell commands are implemented, together with reviewed text patches, recovery,
Git checkpoints/commits, lint correction, and undo. M7 acceptance and installation
checks pass locally; the configured remote platform matrix awaits CI results.

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

See the [configuration schema](.docs/config-schema.md) for all fields. Read and
command approval rules are described below.

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
The scrolling prompt uses a single-row shaded input area, a simple chevron, keyboard hints, and Tab completion
for discovered project paths. Completion lists names only; it does not read or
attach file contents. `/context` shows an approximate token estimate based on
serialized character count; compatible endpoints may use different tokenization
and Skua does not assume a provider context limit. `/compact` asks the model to
summarize older complete turns while keeping the latest two turns in active
context. Compaction is manual, cancellable, and never removes canonical history.
`/help`, `/new`, and `/exit` are available. New/exit/session switching with unsaved
history offers Save / Discard / Cancel. Nothing writes transcripts, notes, or
terminal history files automatically.

The startup animation plays on the first interactive launch for an installation.
Set `[ui].startup_animation` to `"always"` to replay on every launch or `"never"`
to suppress it. Its first-run marker is stored in the user's global state directory
and shared across projects.
It plays in place in the normal terminal buffer, then clears its own rows.
Pure black animation backgrounds use the terminal's default background.

Replies stream as Markdown, with formatted lists, tables, and code blocks.
Diff reviews use full-line green backgrounds for additions, red for deletions,
and muted blue-gray for headers, with neutral text,
including in the scrollable review pager. Partial replies remain marked incomplete in memory
after cancellation or failure. The HTTP client reuses connections, uses a 10-second
connect timeout, 60-second I/O timeout, and 300-second total deadline per attempt.
Temporary transport failures and HTTP 408/429/500/502/503/504 retry up to three times
with 1/2/4-second delays, cancellable with Ctrl-C. Once text or tool arguments
arrive, transport failures end the turn without replaying the partial response.
Authentication/configuration errors fail immediately. The tool loop permits three
correction attempts after an initial malformed response or invalid tool call;
continuing another tool block does not reset that budget.
Responses are limited to 8 MB and individual streamed events to 1 MB.

Protocol and terminal tests use controlled mock responses. A live U-M Toolkit
check also verified streaming chat and native-tool diagnostics after fixing support
for Azure prompt-filter metadata events. Configure an endpoint and run `skua squak`
for a compatibility check with your selected model. Chat sends conversation text,
project guidance, and permitted tool results to the configured endpoint.

## Read tools and approvals

Chat exposes `list_files`, `read_file`, `search_files`, and `run_command`. Calls run
sequentially. Every shell command requires approval; sensitive and outside-root
reads require approval for each operation. Enter defaults to denial; type `y` or
`yes` to approve. Ctrl-C cancels a pending approval. Denial stops further tools
for that turn, and undispatched batch calls are recorded as `not_run`.

Automatic listing/search uses the installed Git CLI's `.gitignore` matcher, even
outside repositories. Explicit files can be read or searched despite ignore rules;
naming a directory does not bypass ignore rules for its descendants. Directory
symlinks are not traversed automatically. Explicit symlinks resolve before review,
and targets are rechecked before reading. Search is case-sensitive literal text.
Reads accept UTF-8 text without NUL, at most 1,000,000 bytes per file. `read_file`
defaults to 200 lines and accepts `start_line` and `max_lines` (up to 2,000).
Results use the configured model-output budget; request a narrower range when cut.
Discovery is capped at 20,000 entries per scan.

Built-in sensitive matches include `.env` and `.env.*` (including example/template
files), `.netrc`, `.npmrc`, `.pypirc`, `.git-credentials`, common SSH private-key
names, `*.key`, `*.p12`, `*.pfx`, `.aws/credentials`, and `.kube/config`.
`[sensitive].filenames` and `.patterns` add protections; empty arrays cannot remove
built-ins. Both lexical and resolved paths are checked. Shell review combines
recognizable sensitive/external paths with execution approval and highlights dot
paths. This inspection is best-effort; scripts and indirect reads can evade it.

Root and nested `AGENTS.md` files refresh at each user turn, including ignored
subtrees. External instruction symlinks and configured sensitive instruction files
are skipped. Guidance is scoped to its directory subtree, remains stable throughout
the turn, and cannot grant permissions. Individual instruction files are capped at
100,000 bytes and the snapshot at 500,000 bytes. Changes and active paths are shown.

The default limit is 20 attempted calls per block, including invalid/denied calls.
Continuing requires permission and authorizes only another block. Normal approval
checks still apply. Command results summarize observed project metadata changes,
including in non-Git projects; `.git` and external paths are excluded. Observation
is bounded and cannot establish a complete audit. A separate reviewed commit offer
is available for attributable command changes; declining leaves them uncommitted.
Use `/output <call-seq>` to inspect retained stdout/stderr without rerunning a
command. Retained output is included when explicitly saving the session.

## Patches, recovery, and Git

The `apply_patch` tool accepts a commit `message` and a `changes` array. Operations
are `write` (`path`, full replacement `content`), `delete` (`path`), and `rename`
(`path`, `destination`, optional replacement `content`). Related changes share one
full diff review. Long reviews open a scrollable pager: arrows/PageUp/PageDown
scroll, `q` returns to the approval prompt, and Ctrl-C cancels. Closing the pager
does not approve. No patch content is silently truncated. Notes use the configured
`notes.directory` unless you choose another destination.

Patches are UTF-8 text only: up to 100 operations, 1 MB per file, and 5 MB each of
original/resulting contents. Reviews exceeding 14 MB when serialized must be split
before approval, keeping approval records within session limits. Sensitive/external
originals require read permission
before diff preparation. Resolved paths and original identities are checked again
before writing. Existing rename destinations are included in the full review.
Binary files, Git metadata, recovery storage, conflicted indexes, and active
merge/rebase operations are rejected. Split a patch that crosses repositories.
External paths in the same repository retain normal outside-root approval rules;
external non-Git files can be patched in a non-Git workflow. Untracked ignored
files cannot be included in managed commits.

In Git projects, patch approval permits the edits; committing is reviewed separately.
After lint, Skua generates a commit subject from the final reviewed diff and shows
the exact message and diff in one commit approval. Command changes also use one
commit approval, separate from command execution. Declining leaves edits uncommitted. Dirty target files first
require a separate checkpoint review of existing working and staged changes.
Declining that checkpoint stops the entire patch. Commits use a temporary index;
unrelated staged and unstaged work stays intact. Skua uses your Git identity and
normal hooks. A successful hook may alter committed content; Skua reports the
configured executable hooks without a second approval. Failed commits keep edits
and hook changes. A subsequent attempt requires fresh review; hooks are never
bypassed or silently retried.

Outside Git, Skua offers `git init` once per session. Declining still permits
reviewed edits, without commit-based undo. Initialization never authorizes a blanket
commit of existing project files.

If enabled, the configured lint command needs its own execution approval. A failed
lint permits three reviewed corrective patches after the initial patch; the group
becomes one commit when lint passes. Exhaustion or refusal leaves edits uncommitted.
`/abandon` clears pending lint grouping without changing files. Pending grouping is
in-memory, so resumed sessions treat remaining edits as existing work and review
any checkpoint anew. Lint changes outside the pending target set are reported but
are not silently included in the patch commit.

Before writing, Skua saves private recovery records under `.skua/.tmp/patch-*.json`.
These contain original/resulting contents and metadata: keep `.skua/.tmp/` ignored
by version control. Partial application attempts conditional restoration; newer
content is preserved and unresolved records remain. Startup offers recovery before
credential lookup or endpoint access; `/recover` offers it again. Successful
application or complete restoration removes its record. Atomic file replacement
and rechecks do not constitute a transaction with arbitrary external editors.

Commands can receive a separate commit offer with a full diff. Ignored artifacts
are excluded. If the repository already had uncommitted work, attribution is
conservatively considered ambiguous and no automatic commit offer is made. Review
concurrent edits in the offered diff. Command commit offers do not automatically
run the patch lint workflow.

`/undo` reviews the latest consecutive Skua change. Checkpoints and intervening
user commits stop traversal. Skua offers a remote fetch first; this requires
approval. Remote-tracking evidence of publication uses a new reversing commit.
Otherwise publication remains unknown, even after a successful fetch:

- `/undo local` explicitly confirms the tip was never shared, and requests removal
  of that change and its file effects while preserving unrelated work.
- `/undo revert` explicitly chooses a reversing commit and preserves history.

Both still require review. Declining fetch does not authorize either strategy.
Repeated reversals skip already-undone changes rather than undoing the reversal.
Dirty targets, merge changes, or changes outside the selected project root stop
undo. Failed undo commits keep inverse edits for inspection. No push or force-push
is performed. Managed commits use `Skua-Metadata` JSON in commit messages as
bookkeeping, not authentication, independently of saved sessions.

## Save, resume, and export

Inside chat:

```text
/save
/save "history/session.jsonl" --name "Bug investigation"
/export
/export "exports/investigation.md"
/sessions
/resume <session-id-or-jsonl-path>
```

From your shell:

```sh
skua sessions
skua --resume <session-id-or-jsonl-path>
skua --root /path/to/project --resume /path/to/saved.jsonl
```

Default saves use `.skua/sessions/<session-id>.jsonl`, or the configured
`sessions.directory`. `/save` updates a resumed session's existing path. Optional
names are metadata; identical names do not collide. Relative destinations resolve
against the project root. IDs are looked up in the configured session directory;
use an explicit path for custom locations or duplicate-ID copies.

Only `/save` or choosing Save at a transition writes JSONL. Failed saves offer
Retry / Save elsewhere / Cancel and keep the conversation and its lock active.
Cancel also stops the pending exit/new/resume action. Discard drops only unsaved
continuation; it does not delete an existing save. Empty or already-saved sessions
skip the transition prompt. Ctrl-D uses the same interactive flow; actual closed
stdin reports unsaved history and exits without writing a file.

Resuming validates the entire record and requires the same resolved project root.
Model/profile/endpoint differences are shown for confirmation before continuing.
A stable OS lock under `.skua/locks/` prevents concurrent use of the same session,
including copies in custom locations. Locks survive atomic saves and release on
exit or process termination; leftover lock files are harmless. Historical tools
and approvals are never replayed or restored as permission grants.

`/export` writes Markdown separately, including unsaved turns. It neither saves
JSONL nor clears the unsaved indicator. It defaults to
`<sessions.directory>/<session-id>.md`. Tool/approval records are escaped inside
closed `<details>` sections. Exports require a `.md` path and cannot replace
canonical session records. Other existing destinations require confirmation;
Skua recognizes its own exports by a session identity marker.

JSONL saves use a temporary sibling file followed by atomic replacement. Changed
destinations are rejected after review. Malformed, truncated, unsupported, or
inconsistent records are rejected with line/field errors and left untouched;
there is no automatic repair. Limits are 64 MB per file, 16 MB per record, and
64 levels of JSON nesting. Saves/exports run only after active turns and their
cancellation cleanup finish. Save files and exports may contain your conversation;
configuration credentials and HTTP headers are not included.

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
guarantee. This is not a filesystem sandbox. Model-requested commands require
approval; there is no separate general command-execution CLI.

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

## Validation

Run `python -m pytest -q` for behavioral and complete workflow checks.
`python scripts/verify_install.py` builds a wheel and verifies it in a fresh venv
with pinned dependencies, outside the checkout. It downloads dependencies unless
`--wheelhouse /path/to/wheels` selects an offline cache.

Local acceptance passed on macOS arm64 with Python 3.12.8 and 3.13.7, Bash/Zsh,
and both streaming modes using mocked endpoints. GitHub Actions is configured for
Ubuntu/macOS and Python 3.12–3.14; those remote jobs have not been verified in this
session. See [VALIDATION.md](VALIDATION.md) for evidence, reproduction commands,
coverage limits, and explicitly opt-in live checks.
