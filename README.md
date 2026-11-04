# Skua

A lightweight, seagull-inspired terminal coding harness for Python 3.12+ on
macOS/Linux, with Bash or Zsh project commands.

**Current status:** interactive editing supports streaming, scoped session
permissions, optional managed Git, lint correction, recovery, and context
compaction. `skua run` adds bounded unattended tasks using the same agent loop.
Interactive history stays in memory until explicitly saved; unattended artifacts
are opt-in. See [VALIDATION.md](VALIDATION.md) for local and reported remote evidence.

Start with [installation](#install-from-this-checkout) and [setup](#setup), then
use [interactive chat](#conversation-and-endpoint-diagnostics) or
[bounded unattended tasks](#bounded-unattended-tasks). For contributors, see
[development](#development), [validation](#validation), and the optional
[Harbor integration](#optional-harbor-integration).

## Install from this checkout

```sh
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
python -m pip install --no-deps .
skua --help
```

Git must be installed and available on `PATH` for file discovery and patches,
even when managed Git is disabled.

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

Use `skua config show --sources` to inspect effective settings and their origins;
the [configuration implementation](skua/config.py) defines all accepted fields
and defaults. Read and command approval rules are described below.

## Conversation and endpoint diagnostics

```sh
skua squak                 # Local checks plus a synthetic endpoint/tool test
skua                       # Interactive chat with the default profile
skua --profile local       # Chat with a named profile
```

Chat startup makes no endpoint request. After you submit a prompt, native tool
responses are validated as needed during that turn; there is no separate automatic
probe. `skua squak` explicitly runs a small synthetic diagnostic that may consume
tokens. It uses a fixed prompt, never project contents, executes no returned calls,
and checks the profile's configured streaming or nonstreaming mode.

Enter sends; Shift+Enter inserts a newline when the terminal sends a distinct
modified-Enter sequence (CSI-u or xterm modifyOtherKeys). Ctrl+J also inserts a
newline. Terminals that send the same carriage return for Enter and Shift+Enter
need a terminal key mapping or the Ctrl+J fallback; Skua does not change terminal
keyboard modes. See the [terminal keyboard protocol](https://sw.kovidgoyal.net/kitty/keyboard-protocol/).

Bracketed pastes of 500+ characters or six+ lines appear as `[N chars]` in the
composer. Their complete text is submitted, with CRLF/CR normalized to LF as for
ordinary pastes. Blocks can be moved past, selected, deleted, or restored with
undo as a single item. History recall keeps the original paste. Typing a label
like `[600 chars]` manually sends that literal text. Smaller pastes and manual
typing remain visible normally; clearing a draft removes its paste blocks from
the submitted input. Paste contents stay in memory with input history and are
not saved separately.

Ctrl-C cancels an active response and
returns to the prompt, or clears an unsent draft. With an empty draft, two
consecutive Ctrl-C presses request exit; Ctrl-D on empty input does the same.
The scrolling prompt uses a shaded, wrapping input area that grows from one to
eight rows, a simple chevron, keyboard hints, and Tab completion
for discovered project paths. Completion lists names only; it does not read or
attach file contents. `/context` shows an approximate token estimate based on
serialized character count; compatible endpoints may use different tokenization
and Skua does not assume a provider context limit. `/compact` asks the model to
summarize older complete turns while keeping the latest two turns in active
context. Compaction is cancellable and never removes canonical history. Automatic
compaction is off by default.
`/help`, `/new`, and `/exit` are available. New/exit/session switching with unsaved
history offers Save / Discard / Cancel. Nothing writes transcripts, notes, or
terminal history files automatically.

To opt in, set an explicit estimated budget in TOML (the value below is an example,
not a detected model limit):

```toml
[context]
budget_tokens = 32000
auto_compact = true
threshold_percent = 80
```

`budget_tokens` is a positive integer; enabling `auto_compact` requires it.
`threshold_percent` is an integer from 1–100 (default 80). `/context` shows these
settings separately from the character-based estimate; provider usage and the
model's actual limit remain unknown. The budget is a target, not a hard request
limit or an output-token allowance. Configure these fields in TOML; setup does not
prompt for them yet.

Before a model continuation, automatic mode compares the estimated request,
including instructions and tools, with the threshold. It attempts compaction at
most once per user turn, retaining two completed turns and the entire active turn.
`/compact` retains the two most recent completed turns. Both preserve tool-call/result
groups, full history, and existing save/resume behavior. Summary requests use the
configured endpoint and can consume tokens; they have a 60-second timeout.

Empty, tool-bearing, oversized, or non-shrinking summaries are rejected. Failure
leaves context unchanged and automatic mode continues without another attempt that
turn; Ctrl-C cancels the turn. Large retained turns/instructions may remain above
the target, and the summary request itself may exceed that target. Skua reports
this condition rather than repeatedly compacting or discarding recent work.
Nothing is automatically saved. `/compact` remains available with automatic mode off.

The startup animation plays on the first interactive launch for an installation.
It uses eased cell transitions at up to 30 fps, with a short entrance and exit,
and finishes in about one second. Playback preserves the terminal background and
crops to small viewports without wrapping animation rows into scrollback.
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
sequentially. Per-operation review is the default for shell commands and patches;
sensitive and outside-root reads always require approval for each operation. Enter defaults to denial; type `y` or
`yes` to approve. Ctrl-C cancels a pending approval. Denial stops further tools
for that turn, and undispatched batch calls are recorded as `not_run`.

Session permissions are opt-in through chat commands:

```text
/permissions
/permissions grant patch src
/permissions grant command "python -m pytest -q"
/permissions revoke 1
/permissions revoke all
```

`grant` shows the proposed scope and requires `y`/`yes`; Enter, denial, or Ctrl-C
adds nothing. `/permissions` lists current grants and their IDs. Revocation takes
effect before the next operation. Grants are cleared on `/new`, successful session
switch/resume, and exit. Saving records the decisions for inspection, but never
restores active grants. Model replies, tool output, project instructions, and
historical approvals cannot create grants.

A **patch grant** permits create, replace, delete, and rename within an existing
project directory recursively. Every source and destination must be in scope;
`src` does not include `src-other`. One grant must cover the entire patch; separate
grants are not combined. Sensitive paths, external targets, Git metadata,
recovery storage, and symlink aliases are excluded. Replacing the grant directory
invalidates its scope. File contents may evolve between operations; each new patch
still captures current contents and checks file and Git identities before writing.
Git initialization, checkpoints, commits, undo, and recovery keep separate reviews.

A **command grant** matches the entire shell string exactly, including whitespace,
quoting, substitutions, pipes, redirects, and compound commands. It is not a prefix
or executable-name grant: adding an argument or `; another-command` requires review.
The resolved invocation, project working directory, shell/startup configuration,
and effective environment must match the grant. Changed environment or replaced
working directory falls back to review. Detected sensitive/external paths and
symlink aliases also fall back to review. Shell inspection is best-effort: commands
can execute changing project scripts, access paths indirectly, and affect files
anywhere. The working directory is not a sandbox or a restriction on side effects.

Matching operations show `Session grant <id>` and record the grant ID as the source
of authorization; they skip the interactive review but retain normal tool progress,
results, cancellation, and output capture. Grants work with either managed Git mode.
Lint uses command authorization, so its exact command can have its own grant.

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

Root and nested `AGENTS.md` files refresh at each user turn. Nested discovery
respects `.gitignore`, skipping ignored files and subtrees such as virtual
environments and caches. The root `AGENTS.md` is loaded even if ignored.
External instruction symlinks and configured sensitive instruction files
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
command. Successful and failed lint output is retained on its patch call as well.
For other tools, `/output` shows the saved tool-result text without rereading files
or calling the model. Existing capture and model-output limits still apply.
Retained output is included when explicitly saving the session.
Command results report exit code, execution time, timeout/cancellation, and any
reviewed commit. Empty streams are omitted; truncation notices distinguish bytes
discarded from bytes still retained outside the preview. Empty reads, file lists,
and searches return an explicit result instead of blank text.

During a turn, a refreshing activity line distinguishes waiting for the model,
waiting for approval, and running a tool. Elapsed time appears only after one
second, and read/list/search operations omit timing entirely. It shows the tool's
call number; approval timing appears in the prompt or diff pager so progress does
not overwrite the review. Tool elapsed time includes its reviews and follow-up
work. Completed tools appear as compact blocks: `Ran <command>`, `Read <path>`,
`Searched <query> in <path>`, or `Updated <paths>`. Commands show up to six preview
lines plus an omission marker; reads, searches, and patches show summaries.
Failures, denials, timeouts, and cancellations have explicit labels. Each block
includes a call number and exit code when available. Read/list/search headings
keep the number on the same line. The `/output <number>` hint appears at most once
per prompted turn; `/help` documents it. Other tools show elapsed time only when
they take at least one second. Long preview lines are clipped only for display;
retained output and model-facing results are unchanged. Ctrl-C cancels
active work and clears progress; captured command output remains inspectable.
Streamed reply text is preserved when switching to reviews and tool summaries.
Activity timing is temporary display state, not saved session history.

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
Binary files, Git metadata, and recovery storage are rejected. Managed Git changes
also reject conflicted indexes and active merge/rebase operations. Split a patch that crosses repositories.
External paths in the same repository retain normal outside-root approval rules;
external non-Git files can be patched in a non-Git workflow. Untracked ignored
files cannot be included in managed commits.

`git.managed` defaults to `true`, preserving the reviewed Git workflow below.
To leave edits uncommitted in Git and non-Git projects, set this in your project
or global configuration (also available through `skua init`):

```toml
[git]
managed = false
```

With managed Git disabled, Skua makes no Git initialization, checkpoint, or commit
offers and requests no commit messages from the model. Editing needs no Git author
identity. Patch review uses the current working contents, including existing dirty
targets; approved replacements can change those contents. Existing staging is left
intact, including staged versions of edited targets, and unrelated work is preserved.
File and Git HEAD/index identity checks still reject changes made during review.
Recovery and separately configured lint remain available. Untracked ignored files
can be edited in this mode. This setting controls Skua's managed workflow; an
approved shell or lint command can still run Git or modify staging.

`/undo` is unavailable while managed Git is disabled, and these uncommitted edits
create no Skua undo entry. You can re-enable the setting to undo eligible earlier
Skua commits, subject to the usual dirty-target checks. Recovery records support
interrupted patches; they are not a general undo history for successful edits.

With managed Git enabled in Git projects, patch approval permits the edits; committing is reviewed separately.
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

With managed Git enabled outside Git, Skua offers `git init` once per session. Declining still permits
reviewed edits, without commit-based undo. Initialization never authorizes a blanket
commit of existing project files.

If enabled, the configured lint command needs its own execution authorization
(explicit review or a matching command grant). A failed
lint permits three reviewed corrective patches after the initial patch; the group
becomes one commit when lint passes if managed Git is enabled in a repository. Exhaustion or refusal leaves edits uncommitted.
`/abandon` clears pending lint grouping without changing files. Pending grouping is
in-memory, so resumed sessions treat remaining edits as existing work and review
any checkpoint anew when managed Git is enabled. Lint changes outside the pending target set are reported but
are not silently included in the patch commit.

Before writing, Skua saves private recovery records under `.skua/.tmp/patch-*.json`.
These contain original/resulting contents and metadata: keep `.skua/.tmp/` ignored
by version control. Partial application attempts conditional restoration; newer
content is preserved and unresolved records remain. Startup offers recovery before
credential lookup or endpoint access; `/recover` offers it again. Successful
application or complete restoration removes its record. Atomic file replacement
and rechecks do not constitute a transaction with arbitrary external editors.

With managed Git enabled, commands can receive a separate commit offer with a full diff. Ignored artifacts
are excluded. If the repository already had uncommitted work, attribution is
conservatively considered ambiguous and no automatic commit offer is made. Review
concurrent edits in the offered diff. Command commit offers do not automatically
run the patch lint workflow.

With managed Git enabled, `/undo` reviews the latest consecutive Skua change.
Checkpoints and intervening user commits stop traversal. Strategy selection never
fetches, inspects remote-tracking refs, or infers whether a change was published:

- `/undo` and `/undo revert` create a new reversing commit, preserving history.
- `/undo local` explicitly requests removal of an eligible current tip and its
  file effects, moving HEAD to its parent (or an unborn branch for a root commit).
  It preserves unrelated work and never switches strategy based on remote refs.

Both strategies show the selected effects and inverse diff for approval. A local
rewrite requires the target at the current tip; use a reversing commit for earlier
changes in a reversal chain. Reversing commits also show the exact commit message.
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

`run_command` accepts optional `timeout_seconds` (alias `timeout`) as a positive,
finite number of seconds. The effective timeout is capped by the configured
command timeout and the remaining unattended task time. Conflicting aliases,
strings, booleans, and nonpositive values are rejected before execution. Omitting
both uses the configured limit. Each result records requested, configured, and
effective timeouts; this does not change model, tool-call, or dollar budgets.

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

Pins are generated with Python 3.12. Platform/Python coverage is recorded in
[VALIDATION.md](VALIDATION.md); the full macOS/Linux matrix has not been verified
locally. Tests use temporary homes/projects and do not need an endpoint or real key.
The optional Harbor tests use a separate environment and must be invoked explicitly;
see the [integration setup](integrations/harbor/README.md).

Keep local credentials, project configuration, exported notes, and run artifacts
out of commits. The ignore rules cover `.env` variants (except `.env.example`),
`.skua/config.toml`, default session/recovery/cache directories, and `.notes/`.
Use `.skua/.tmp/` for local bundles, benchmark data, and unattended result/transcript
files; custom output locations need their own ignore rules. Before pushing, review
`git status --short --untracked-files=all` and `git diff --check`. Ignore rules do
not remove files that are already tracked.

## Bounded unattended tasks

Configure a model with `skua init` or a config file, then supply task text through
a UTF-8 file or stdin. Authentication still uses the configured environment
variable or project `.env`; there is no command-line credential option.

```sh
skua run --root /path/to/workspace --prompt-file /path/to/task.txt \
  --policy disposable --max-seconds 300 --max-tool-calls 100 \
  --result-file /path/to/new-result.json --transcript-file /path/to/new-history.jsonl

cat task.txt | skua run --root /path/to/workspace --prompt-file - --policy review
```

`--policy` is required. `review` permits existing unreviewed read/list/search
operations, then stops at the first action requiring approval. `disposable`
authorizes ordinary patches and entire shell commands without asking. Detected
sensitive or external access still returns `approval_required`. Shell commands can
execute project code and access arbitrary files: **policy is not a sandbox**.
Use an externally isolated, disposable environment for unattended commands.
`container` additionally authorizes reads, patches, and commands referencing paths
outside the project root. Select it only inside an externally isolated container
without host credentials or writable host mounts; it does not create or verify
isolation. Detected sensitive paths still require approval. Harbor selects this
policy explicitly; interactive defaults and `disposable` remain unchanged.
Command-path detection is heuristic. Python heredoc bodies are scanned for quoted
path literals rather than treating arithmetic punctuation as shell arguments.
An optional Harbor adapter is available under
[integrations/harbor](integrations/harbor/README.md); its synthetic Docker
acceptance checks passed locally.

Managed Git is **off for headless runs**, independently of the interactive config.
Add `--managed-git on` to opt into initialization/checkpoints/commits; the
`disposable` and `container` policies can authorize these actions automatically. Existing author,
identity, staging, lint, and recovery checks still apply. Unmanaged runs leave
edits uncommitted. No headless policy prompts for input, Git approval, or recovery.
If `reason` is `recovery_pending`, resolve the unfinished patch through interactive
Skua in that workspace, inspect the resulting files, then retry the task.

`--max-seconds` defaults to 300 and must be positive and finite. It covers execution
after the task has been read, including model requests/retries, tools, and context
handling. Stdin must end at EOF. At the deadline, active work is cancelled and
cleanup is awaited; process termination or an in-flight filesystem/Git mutation
can extend wall time beyond the deadline. Git subprocesses retain their existing
60-second timeout. This is an execution deadline, not a hard process-kill timer.
SIGINT and SIGTERM use the same cleanup path.

`--max-tool-calls` defaults to 100 and counts model-requested calls across all
responses, including invalid and denied calls. Internal lint/Git steps belong to
the initiating call and remain subject to the time limit. The explicit total
budget replaces interactive block-continuation prompts; no further calls execute
once exhausted. A final model response without tools can complete at the exact
limit. Unexecuted calls in an already received batch are recorded as `not_run`.

Unattended runs provide the model with refreshed working-directory, interpreter,
package-manager, build-tool, and budget context before each planning call. Discovery
only checks executable paths; it does not run programs, import modules, install
dependencies, or execute activation scripts. PATH results describe the environment
before shell startup/activation and may differ from the configured task environment.
The context identifies Skua's Python separately and directs the agent to check task
prerequisites with the selected task interpreter.

Remaining time, remaining tool calls, configured command timeout, and output limits
are visible to the model. Command dispatch records the effective ceiling (the smaller
of remaining task time and command timeout); tool results include remaining time.
These snapshots do not extend the existing shared deadline or grant permissions.
Optional transcripts retain `runtime_context` and `command_budget` notices for review.

Unattended guidance asks the agent to perform the requested work, choose an observable
acceptance check, and verify the resulting state before concluding. Creating a setup
script is an intermediate step when the task asks for a configured service; writing
an answer file does not validate a failed calculation. Advice-only and code-only
requests retain their scope. The final response should describe observed checks and
remaining blockers. This is model guidance, not an automatic correctness gate, and
does not add retries, permissions, or budget.

Stdout contains one JSON outcome object; streamed model/tool content is retained
in the optional transcript, not mixed into stdout. The `message` and `next_step`
fields explain the outcome and follow-up in plain language; automation should use
the stable `status`, `reason`, and `exit_code` fields. Exit codes are:

| Status | Exit code | Meaning |
| --- | ---: | --- |
| `completed` | 0 | The agent turn ended normally, with no pending lint correction or final tool error |
| `failed` | 1 | Configuration, execution, endpoint, input/artifact error, or unresolved final tool error |
| `approval_required` | 3 | Policy requires interactive review, or recovery must be resolved |
| `budget_exhausted` | 124 | Total time or tool-call budget reached |
| `cancelled` | 130 | Execution cancelled, including SIGINT/SIGTERM |

Invalid CLI syntax/limits use argparse's exit code 2 and stderr usage text.
A completed turn **does not verify task success**: `task_success_verified` is
always false, and `provider_usage` remains null. External tests/verifiers must
establish correctness. Earlier tool errors can be corrected during the same turn;
inspect the transcript for the full sequence.

`diagnostics` contains `model_refusal` when the endpoint explicitly supplies a
nonempty refusal field; the transcript retains a matching notice. Refusal prose
without that field is not automatically classified. This diagnostic does not turn
a normally completed response into a transport error or trigger a retry. It does
not establish task success; external evaluation remains separate.

`--result-file` stores the same JSON metadata as stdout; `--transcript-file`
stores native JSONL history. Paths are relative to the invoking directory, require
existing parent directories, and must not already exist. Files are created with
mode 0600; an existing file or symlink is never overwritten. Preflight failure may
leave a reserved artifact empty (for example, no session exists when recovery
blocks startup). `input_or_artifact_error` means to check task readability/UTF-8,
the 1 MB task limit, destination existence, and parent-directory permissions.
Configuration failure before artifact setup returns JSON on stdout only.

Outcome metadata omits task text, paths, endpoint/model identity, credentials, and
raw exception messages. The optional transcript contains task/model/tool data and
can be sensitive; the resolved API credential is redacted, but this is not a
universal secret scrubber. Redaction can alter identity fields, so a redacted
transcript is not guaranteed to be resumable. No artifact or transcript is saved unless requested.
Approval records use `authorization.source = "unattended_policy"`, with no
reusable grant or credential/environment snapshot persisted.

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


### Compare interactive modes offline

Run `python scripts/interactive_acceptance.py` from this checkout to compare the
same edit → failed lint → corrective edit workflow with network access blocked.
It uses temporary projects and mocked responses, preserving unrelated staged and
unstaged files. The fresh-install checker also runs this comparison for each
available Bash/Zsh and streaming/nonstreaming combination.

| Selected mode | Operation reviews | Grant confirmations | Total authorization prompts | Commits | Transcript saves |
| --- | ---: | ---: | ---: | ---: | ---: |
| Default: managed Git, per-operation review, automatic compaction off | 5 | 0 | 5 | 1 reviewed | 0 |
| Smoother: unmanaged Git, scoped patch + exact lint-command grants, automatic compaction on | 0 | 2 | 2 | 0 | 0 |

The five default reviews cover two patches, two lint commands, and the final
commit. The smoother count includes both initial grant confirmations; subsequent
matching operations reuse those grants. Both modes also ask whether to save or
discard on exit; that common exit prompt is excluded from authorization counts.
These counts describe this fixture, not every task. Sensitive/external targets,
Git actions, and recovery still require their own reviews. Grants expire with
the session, and exact-command authorization does not sandbox the shell.

The fixture seeds five earlier turns and uses a deliberately tiny 100-token
estimated budget to exercise automatic compaction once in smoother mode. This
is a test trigger, not a recommended budget or a model limit. Real usage requires
an explicit budget suited to your model; see the context configuration above.

For a hands-on display check, run both commands in a terminal and follow their
printed instructions:

```sh
python scripts/interactive_acceptance.py --interactive
python scripts/interactive_acceptance.py --interactive --smoother
```

Approve the fixture reviews, inspect the incremental response text, diff display,
and elapsed lint progress, then enter `/context` and `/exit`, choosing `discard`.
The script verifies final content, authorization counts, Git state, compaction,
and absence of transcript saves. It does not measure real-provider behavior.


### Optional Harbor integration

The [Harbor adapter](integrations/harbor/README.md) uses a separate Harbor 0.25.0
host environment, a SHA-256-pinned Skua wheel, and validated ATIF-v1.8 exports.
The synthetic container suite requires Docker; it can also be run through the
manual [Harbor integration smoke](.github/workflows/harbor.yml) workflow after
pushing. Recorded local Docker trials passed for verified success, failure
artifacts, and timeout artifacts; remote workflow execution remains unverified.

The [Terminal-Bench harness](integrations/harbor/benchmark/README.md) adds dataset
selection, a host-side budget gateway, pilot gates, and artifact reports. Paid
partial evaluations were run and stopped; they do not establish a complete
benchmark score. See the [stopped-run review](integrations/harbor/benchmark/REVIEW.md)
and [validation record](VALIDATION.md) for historical results and limitations.
Keep real-run artifacts local; the CI workflow uses synthetic fixtures.
