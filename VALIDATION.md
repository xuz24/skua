# Validation and release evidence

## macOS CI cleanup regression — 2026-10-25

Local verification: **371 passed** on Python 3.12.8; **46 activity/execution tests
passed** on Python 3.13.7. The 3.13 run used the existing pinned dependency directory
via `PYTHONPATH`, not a fresh 3.13 installation. `git diff --check` passed.

User-provided CI logs report two failures on macOS/Python 3.12 (timeout and
cancellation activity) and one on macOS/Python 3.13 (cancellation activity), against
the 347-test checkpoint. These are actual failed remote runs, not an unverified
matrix configuration. The logs do not include the underlying swallowed exception.

A local subprocess reproduction confirms that macOS can raise `EPERM` from
`killpg(pid, 0)` for an exited but unreaped group. Apple's
[killpg1 implementation](https://github.com/apple-oss-distributions/xnu/blob/main/bsd/kern/kern_sig.c)
excludes zombie group members and can return `EPERM` when none remain signalable.
This is consistent with the reported statuses: cleanup previously propagated the
exception into the generic tool-error handler, losing timeout/cancellation results.

Cleanup now yields and retries Darwin `EPERM` for at most 250 ms per group signal
or probe. It continues signalling groups (including surviving descendants), and
persistent permission errors or other errors remain errors. Regression tests cover
TERM/probe/KILL retries, bounded real permission errors, real macOS zombie/reaping,
and retained output/activity status through timeout and cancellation. CI assertions
now include result details on failure. A remote rerun is still required to confirm
these particular workflow failures are resolved.

## Next-version M4 — automatic context management

Local validation on 2026-10-25: **362 tests passed** with
`.venv/bin/python -m pytest -q`; `git diff --check` passed. Fresh offline wheel
installation passed with cached dependencies, `pip check`, both entry points,
and Bash/Zsh × streaming/nonstreaming mocked acceptance workflows.

`tests/test_context.py` checks explicit budget/threshold validation, automatic
triggering before model continuation and after a large tool result, retention of
recent turns and complete tool groups, and one compaction attempt per turn.
It covers malformed/oversized/non-shrinking summaries, timeouts, cancellation,
manual `/compact` with automatic mode off, and save/resume without implicit saves.
Provider usage and model limits remain unknown; the budget is an estimate target.
No live-provider or remote-platform validation is claimed.

## Next-version M3 — scoped session permissions

Local validation on 2026-10-21: **347 tests passed** with
`.venv/bin/python -m pytest -q`; `git diff --check` passed. Fresh offline wheel
installation passed with the cached runtime wheelhouse, including `pip check`,
both entry points, and all Bash/Zsh × streaming/nonstreaming acceptance workflows.

`tests/test_permissions.py` covers repeated granted operations, default review,
revocation through terminal controls, scope boundaries, rename destinations,
sensitive/external targets, symlinks, directory replacement, and stale file checks.
Exact-command checks include whitespace, substitutions, pipelines, compound
commands, and changed environments/startup configuration. Granted command
cancellation retains output and marks the remaining batch as not run.

Pipe-input and scripted terminal checks exercise Ctrl-C/denial during grant
confirmation, changed scope during confirmation, listing/revocation, `/new`, and
save/resume. Policy authorization is recorded distinctly; historical approvals and
model text grant nothing. Existing v1 records without authorization metadata load,
and malformed metadata is rejected. No live provider or remote CI is involved.

## Next-version M2 — optional managed Git

Local validation on 2026-10-21: **306 tests passed** with
`.venv/bin/python -m pytest -q`; `git diff --check` passed.

Completion checks in `tests/test_managed_git.py`, `tests/test_patches.py`, and
`tests/test_setup.py` cover:

- Git/non-Git edit/test/fix with managed Git disabled, no author identity or
  commit-message requests, no initialization/checkpoint/commit offers, preserved
  target staging and unrelated working changes, and separate lint approval.
- Unmanaged command changes, ignored-file edits, configuration defaults and
  inheritance, and documented undo unavailability in disabled mode.
- File, HEAD, and index changes invalidating review; partial-patch restoration;
  approved recovery in both modes while preserving newer user edits.
- Default/explicit reversing undo and explicit local rewrites, with Git invocation
  guards rejecting fetch and remote/publication inspection even when configured
  remotes and remote-tracking refs exist.
- Undo denial/cancellation, dirty targets, checkpoints, consecutive changes,
  reversal chains, and unrelated staged/unstaged work.

Fresh offline wheel installation passed using the cached runtime wheelhouse,
including `pip check`, both entry points, and Bash/Zsh × streaming/nonstreaming
mocked acceptance workflows. The installed acceptance script now reviews undo
without a fetch prompt. No remote CI or live provider was used.

## Next-version M1 — tool progress and feedback

Local validation on 2026-10-21: **283 tests passed** with
`.venv/bin/python -m pytest -q`; `git diff --check` passed.

M1 completion checks are in `tests/test_activity.py`:

- A real slow shell command refreshes its elapsed display while the event loop
  remains responsive; SIGINT cancels it, retains output, and restores the handler.
- Success, failure, timeout, denial, invalid calls, unexpected exceptions, and
  cancellation finish the activity lifecycle without leaving an active display.
- Ctrl-C cancels both short patch review and the full diff pager without editing
  the target or leaving a spinner active.
- A mocked streamed edit/test/fix turn runs real patches and lint, renders both
  diffs, preserves all reply text exactly once, and retains failed and successful
  lint output for `/output`.
- Model waiting restores a surrounding tool operation; approval elapsed time
  appears in the prompt toolbar. Existing `/output` checks verify no re-execution.

These are automated local terminal/pipe-input checks, not a manual terminal
walkthrough or live-provider validation. No M2 Git workflow changes are included.

## Local v1.0.0 checkpoint — 2026-10-21

- Full local suite: **272 passed** after the final input and diff-highlight fixes.
- Fresh offline wheel installation: passed, including pinned dependencies,
  `pip check`, both CLI entry points, and all Bash/Zsh × streaming/nonstreaming
  acceptance scenarios. The installed CLI reports `skua 1.0.0`.
- `git diff --check`: passed.
- Earlier focused runs encountered intermittent command timeout/cancellation
  failures that passed on rerun; the final full suite passed without retries.
- Remote CI remains deferred. These results establish local macOS behavior,
  not complete platform coverage or live-provider acceptance.

As of 2026-10-21, milestones 1–6 and the requested interface/context/commit
follow-up are implemented. M7's acceptance workflow,
installation checker, documentation checks, and CI configuration are implemented.
The remote platform matrix still needs to run before claiming complete v1 platform
validation. These checks do not publish, deploy, push, or contact a live model.

## Recorded results

| Environment | Check | Result |
| --- | --- | --- |
| macOS arm64, Python 3.12.8 | Full pytest suite for v1.0.0 | 272 passed |
| macOS arm64, Python 3.12.8 | Fresh venv, pinned runtime installation, installed wheel | Passed |
| macOS arm64, Python 3.13.7 | Fresh venv, pinned runtime installation, installed wheel | Passed |
| Both fresh environments | Bash and Zsh × streaming and nonstreaming | All four combinations passed per interpreter |
| Both fresh environments | `pip check`, console entry point, `python -m skua` | Passed |
| GitHub Actions: Ubuntu/macOS × Python 3.12/3.13/3.14 | Full suite and fresh installation | Configured; not yet run in this session |
| Real Conda, other Python versions, real-provider patch workflows | Additional compatibility checks | Not validated by this milestone |

The follow-up added coverage for startup-asset playback/state, session compaction
round trips, path completion, final commit-message reviews, and installed-wheel
asset packaging. No remote CI or live endpoint checks were run for this update.

The source suite's actual prompt tests cover paste, submission, cancellation, and
pager closure without approval. The full workflow uses scripted terminal answers
and HTTPX MockTransport; it is not a manual interactive/live-provider test.

## Complete workflow

`scripts/installed_smoke.py` and `tests/test_end_to_end.py` share one scenario:

1. Create global configuration in an isolated temporary home through the setup wizard.
2. Validate configuration and profiles; run local and synthetic endpoint diagnostics.
3. Start chat, load project guidance, list/read/search files, and approve a command.
4. Review two patches, lint commands, and generated commit messages, producing
   separate managed commits while preserving unrelated staged and unstaged work.
5. Save explicitly, exit, resume, and inspect retained output without rerunning it.
6. Deny a sensitive read and run an approved command that fails. Verify both outcomes
   survive in history and the sensitive fixture value never reaches the endpoint.
7. Export Markdown, explicitly choose reversing undo twice,
   save, and exit. Verify original target contents and unrelated staging remain intact.

Both streaming modes use the real ChatClient parser. Python socket connection
attempts are blocked. Git operations and shell effects stay in disposable projects;
no remote is configured. Separate behavioral tests cover local remotes, refusal,
stale approvals, hooks, recovery, malformed records, and cancellation.

## Reproduce locally

From a development environment installed using `requirements-dev.txt`:

```sh
python -m pytest -q
python scripts/verify_install.py
```

The second command builds a wheel, creates a fresh venv, installs exactly
`requirements.txt`, installs the wheel, runs `pip check` and both entry points,
then copies the acceptance script outside the checkout and runs all available
shell/streaming combinations. Runtime installs need package-index access by default.
Temporary environments, projects, and homes are deleted when the checker exits.
It does not install into your active environment or change user configuration.

To use already downloaded wheels without network access:

```sh
python scripts/verify_install.py --wheelhouse /path/to/runtime-wheels
python scripts/verify_install.py --python /path/to/python3.13 --wheelhouse /path/to/runtime-wheels
```

The wheelhouse must contain every pinned runtime dependency for the target
platform/interpreter. The build interpreter still needs the development build
requirements installed. Local M7 runs reused the existing macOS wheelhouse under
`.skua/.tmp/milestone1-wheels/`. Logs were captured as `/tmp/skua-m7-install312.log`
and `/tmp/skua-m7-install313.log`; these are disposable local evidence, not required
runtime files.

## CI

`.github/workflows/ci.yml` runs on pull requests, pushes, and manual dispatch. It
uses a six-job Ubuntu/macOS and Python 3.12–3.14 matrix, installs Zsh on Linux,
installs pinned development dependencies, runs the suite, and verifies a fresh
wheel installation. Only read access to repository contents is requested; checkout
credentials are not persisted. There is no publishing job or live-endpoint secret.

The action versions and inputs follow the official
[checkout](https://github.com/actions/checkout) and
[setup-python](https://github.com/actions/setup-python) usage documentation. A
configured matrix is not evidence that its jobs passed. Review actual CI results
before declaring all target platforms verified.

## Dependencies and examples

No runtime dependency was added for M7. All five direct runtime packages have an
active purpose: HTTPX for transport, prompt_toolkit for input/paging, Rich for plain
terminal rendering, TOML Kit for comment-preserving setup edits, and python-dotenv
for explicit project `.env` parsing. The cached macOS wheelhouse's 15 pinned
runtime dependency wheels total 3,314,064 bytes; this is compressed download size,
not installed disk usage or a cross-platform size guarantee. `pip check` passed in
both fresh installations. Runtime pins were retained, not silently upgraded.

README TOML examples are parsed and merged through the production configuration
validator in the test suite. Internal `.docs` files are locally ignored by this
repository; they are synchronized for handoff, but CI does not depend on them.

## Optional live checks and remaining release work

Live endpoint diagnostics are explicitly opt-in because they consume tokens. Run
`skua --profile <profile> squak` only for a profile you intend to test. The diagnostic
uses synthetic content and executes no returned tool calls. Repeat for a local and
a hosted compatible endpoint when available. Real edit workflows should use a
throwaway project and explicit review of every operation. Routine CI does neither.

Prior M3 work verified U-M Toolkit streaming and native-tool diagnostics; it does
not establish M7 live patch-workflow compatibility. The remaining release gate is
actual successful CI results across the configured platform matrix. Additional
live endpoint and real Conda checks should be recorded separately when performed.
