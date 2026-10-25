# Validation and release evidence

## Next-version M5 — interactive acceptance complete

Local validation on 2026-10-25, macOS arm64 / Python 3.12.8:
**375 tests passed** with `.venv/bin/python -m pytest -q`. The four new comparison
cases also passed after adding rendered-progress/output-link assertions.
Fresh offline installation passed with pinned dependencies, `pip check`, both
entry points, and the old and new mocked acceptance workflows under
Bash/Zsh × streaming/nonstreaming. `git diff --check` passed.

Reproduce with:

```sh
.venv/bin/python scripts/interactive_acceptance.py
.venv/bin/python -m pytest -q
.venv/bin/python scripts/verify_install.py --wheelhouse .skua/.tmp/milestone1-wheels
```

`tests/test_interactive_acceptance.py` runs the same edit → failed lint → fix
through the terminal conversation and actual ChatClient with mocked HTTP. Real
socket connections are blocked. Streaming replies arrive in delayed text deltas;
the lint command runs long enough to render elapsed progress. The scenario seeds
five old turns and verifies that automatic compaction occurs exactly once in
smoother mode, without deleting canonical history. A fixture-only 100-token budget
forces that trigger; it is not a recommended model budget.

| Configuration | Operation reviews | Initial grant confirmations | Total authorization prompts | Commits | Transcript saves |
| --- | ---: | ---: | ---: | ---: | ---: |
| Managed Git; per-operation review; auto-compaction off | 5 | 0 | 5 | 1 reviewed | 0 |
| Unmanaged Git; patch-directory and exact lint-command grants; auto-compaction on | 0 | 2 | 2 | 0 | 0 |

The default reviews are patch, lint, corrective patch, lint, and commit. Counts
include smoother-mode grant setup; the common save/discard exit prompt is excluded.
Both scenarios assert final content, failed/successful tool outcomes, retained
output links, cleared activity, serializable history, and preservation of unrelated
staged and unstaged files. Neither scenario requests a save. These are measured
fixture results, not a general guarantee of prompt reduction for every task.

The passing regression suite also covers the M5 interruption/recovery checks:

- Model response: `test_session.py::test_cancel_ends_turn_and_keeps_partial_text`
  and client stream cancellation/cleanup in `test_client.py`.
- Approval: Ctrl-C on short/full review in `test_activity.py` and undispatched
  calls marked not-run in `test_tools.py`.
- Command execution: real SIGINT, timeout, repeated cancellation, child cleanup,
  retained output, and granted-command cancellation in activity/execution/permission tests.
- Compaction: manual SIGINT and automatic cancellation preserve prior context and
  clear activity in `test_context.py`.
- Partial patch recovery: restoration on injected write failure in `test_patches.py`
  and `test_managed_git.py`; recovery remains explicitly reviewed in both Git modes.

**Manual M5 gate passed (user-reported, 2026-10-25):** the user ran both
walkthroughs below and confirmed “both ran correctly and everything was readable.”
This confirms the requested streaming, diff-review, and lint-progress readability
check in both modes. No blocking interactive defect was reported. Together with
the automated and fresh-install checks, this completes M5. M6 has not started.

```sh
.venv/bin/python scripts/interactive_acceptance.py --interactive
.venv/bin/python scripts/interactive_acceptance.py --interactive --smoother
```

Follow the printed commands, approve the fixture reviews, then choose `discard`
at exit. Each walkthrough uses an isolated temporary project and mocked provider.
No live-provider acceptance or remote CI run of these M5 additions was inspected.
The previously reported passing CI rerun below predates these additions.

## macOS CI cleanup regression — 2026-10-25

After pushing the fix, the user confirmed that all CI rerun jobs passed, including
the previously failing macOS jobs. The regression is resolved in the reported
rerun. This is user-reported remote validation; run URLs and commit IDs were not
provided or independently inspected. Earlier checkpoint limitations below remain
historical; manual M5 acceptance has since passed as recorded above. Live-provider
acceptance remains outstanding.

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
now include result details on failure. The user subsequently reported that all
remote rerun jobs passed.

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
