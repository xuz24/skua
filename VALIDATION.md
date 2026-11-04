# Validation and release evidence

## Smoother startup transitions — 2026-10-27

The seven original poses now use deterministic eased cell dissolves at up to
30 fps, with 150 ms entrance/exit transitions and a one-second total timeline.
Rendering time counts against that timeline; late frames are skipped. Palette
colors and the terminal's default background are preserved. Playback uses the
chat console and crops both axes to avoid changing layout on small terminals.

**37 focused startup/follow-up/terminal tests passed**, including unchanged
once/always/never behavior, cancellation and cursor restoration, monotonic
transitions, original-pose retention, blank final frame, and narrow viewport
geometry. Timed true-color ANSI playback completed in 1.03 seconds with the cursor
restored. Physical terminal-emulator visual inspection was not performed.
`git diff --check` passed.

## Final text-only run readiness — 2026-10-27

**465 core tests and 50 Harbor integration tests passed.** The gateway tests used
local mock servers; socket access required execution outside the sandbox. A fresh
bundle was built with the trusted standalone Linux amd64 Python archive. Wheel
SHA-256: `bf2fa952cbf333041c9f34c3cd4cec79951f84949f85f534f47ed91c13aaf8e2`.

Actual Docker/Harbor trials on Docker Desktop 29.9.2 (Apple Silicon host,
`DOCKER_DEFAULT_PLATFORM=linux/amd64`) passed verified success, retained failure,
and retained timeout acceptance. A fourth synthetic trial received real SIGTERM
while its container was running. Harbor exited without forced killing; trial
artifacts survived and no owned containers, networks, or volumes remained.
Artifacts: `.skua/.tmp/harbor-readiness-20261027/{bundle,smoke,interrupt}/`.

Readiness changes include graceful scheduling stop with a retained interrupted
row, bounded process termination, ownership-scoped resource cleanup, a fixed
capability-diverse pilot with at least one required verifier pass, and matching
pilot/full bundle, harness source, model, endpoint, platform, and limits. Reports
retain secondary failure flags; gateway ledgers retain allowlisted finish reasons
and reject non-text input before budget reservation. Forced termination and
cleanup-failure reporting are covered by offline tests; actual Docker acceptance
exercised graceful cancellation. `git diff --check` passed.

The fresh `.skua/.tmp/harbor-readiness-20261027/data/` directory contains validated
dataset inputs and a link to the unchanged source checkout; it has no paid phase
directories or ledgers. Selection preview confirms 82 eligible tasks and the
fixed three-task capability pilot. No paid diagnostic, pilot, or full run was
started. Live connectivity, current provider credit, and a passing matching
pilot remain prerequisites; offline readiness is not live task-performance evidence.

## Text-only Harbor subset — 2026-10-27

The benchmark runner now defaults to `--subset text-only`: 82 of the pinned 89
tasks. Seven exclusions and their reasons were reviewed from public instructions,
without reading solutions or verifier tests. Text-specified image-processing
programming tasks remain included. `--subset all` restores the original cohort.
`--list-tasks` validates the dataset and prints a preview before credentials,
bundle validation, pilot checks, Docker, or model requests. The real pinned local
dataset preview returned 82 selected tasks and seven exclusions.

**34 Harbor adapter/reporting tests passed**, including selection ordering,
retained image-programming tasks, changed-dataset rejection, pilot subset matching,
preview without execution, reporting exclusions separately from unrecorded tasks,
and unchanged per-task spending allowances. Full-run task allowances total $13.12
for this subset under the unchanged $14.24 phase ceiling. `git diff --check`
passed. No paid benchmark was launched and historical artifacts were not changed.

## Unattended execution and verification guidance — 2026-10-27

Unattended planning context now asks for the requested outcome, an observable
acceptance check, correction of failed checks within existing limits, and an
honest final account of checks and blockers. Advice/code-only scope is preserved.
Provider refusal fields are retained as `model_refusal` notices and safe result
diagnostics, without correction retries or transport-error classification.
Ordinary prose is not heuristically classified as a refusal.

**465 core tests passed**, including offline fixtures contrasting a setup script
with applied configuration and an answer write after SyntaxError with a repaired,
checked calculation. Fixtures use real tools and scripted model replies; they
validate harness behavior, not live model compliance. Streaming and nonstreaming
refusal metadata, no-retry behavior, and unverified external-success semantics
are covered. **28 Harbor adapter/reporting tests passed**. `git diff --check`
passed. No paid benchmark or live model evaluation was run; impact on Harbor
success rates remains unmeasured.

## Shift+Enter and collapsed paste input — 2026-10-27

Shift+Enter's CSI-u/xterm sequences now insert a newline; Ctrl+J is the fallback.
Plain Enter still submits. Terminal keyboard modes are not changed; terminals
that encode Shift+Enter identically to Enter require a key mapping or Ctrl+J.
Input wraps and grows from one to eight rows. Bracketed pastes of 500+ characters
or six+ lines display as `[N chars]`, with complete normalized text returned on
submission. Manual input is never collapsed based on length.

**458 core tests passed**. Coverage includes Shift+Enter escape sequences, newline
fallback, wrapping manual input, Unicode character counts, preserved full paste
submission/history recall, atomic deletion/undo, draft clearing, multiple blocks,
literal marker text, and scoped restoration of key mappings. A rendered paste
example was inspected. Physical terminal-emulator key mappings were not exercised.
`git diff --check` passed.

## Deferred tool validation and quieter output — 2026-10-27

Chat startup/resume no longer sends a synthetic tool-support probe. Submitted
turns validate actual tool responses; explicit `skua squak` diagnostics retain
their probe. Opening help and exiting performs no model request. The shared
start/resume acceptance workflow now makes 13 mocked requests instead of 15.

Read/list/search headings keep the output number inline and omit timing. Other
activity timers appear only after one second. `/help` explains `/output <number>`;
the automatic inspection hint appears at most once per prompted turn.

Validation: 444 tests passed in the full core run; five old startup/resume probe
expectations were then updated. All **65 affected regression tests passed** on
rerun, including end-to-end Bash/Zsh, streaming/nonstreaming, startup/resume,
timing thresholds, output hint reset, and cancellation. `git diff --check` passed.

## Unattended environment and time context — 2026-10-27

- **442 core tests passed**; **28 Harbor adapter/reporting tests passed**.
  After the context-estimate adjustment, the context/discovery regression suites
  also passed. `git diff --check` passed.
- Planning calls receive cwd, configured shell/environment, PATH-discovered
  interpreters/package managers/build tools, separate agent-Python identity, and
  remaining time/calls plus output limits. Discovery invokes no programs or
  activation scripts and explicitly distinguishes PATH hints from verified task
  capability. Optional native transcripts retain the supplied runtime context.
- Command dispatch records the effective time ceiling; tool results expose
  remaining time. Existing deadline/cancellation and total-call enforcement remain.
  Offline tests cover missing prerequisite setup/verification, PATH refresh,
  private-runtime distinction, credential exclusion, and non-extending budgets.
- Benchmark `--max-seconds` is explicit and recorded, defaults to 120 seconds,
  and leaves dollar caps unchanged. Harbor's outer timeout follows the selected
  ceiling plus cleanup allowance. Full runs require a matching pilot time setting.
- No paid pilot, Docker run, or native-amd64 comparison was performed. Real-task
  effectiveness and the broader pilot capability gate remain unverified/pending.

## Compact terminal tool blocks — 2026-10-27

Implemented the selected CLI presentation: command/path headings, short indented
command previews, summaries for reads/searches/patches, explicit failure labels,
and elapsed time/exit status/retained-result references. `/output` also opens saved
non-command results without another model request or file read. Preview clipping
does not alter model-facing results, retained captures, or saved session schemas.

**438 core tests passed**, including streaming/review ordering, live progress and
cancellation cleanup, command preview truncation, denied execution, retained read
inspection, and Bash/Zsh interactive acceptance. Inspected representative plain
terminal blocks. `git diff --check` passed. No paid model or benchmark runs.

## Instruction discovery in large projects — 2026-10-27

Nested `AGENTS.md` discovery now respects project ignore rules, preventing ignored
virtual environments and caches from exhausting the discovery limit before a
model turn. The root instruction file remains an explicit exception even if
ignored. Entry/content limits and sensitive-file/symlink checks remain enforced.

Validated against `/Users/zijiexu/projects/nl_image_editor`: instruction loading
succeeds with its existing `.gitignore`, without scanning the 38,017-entry `.venv`.
No files in that project were changed. **76 tool/headless tests passed**, including
an oversized ignored environment, root guidance excluded by ignore rules, nested
ignore negation, and successful turn startup. `git diff --check` passed.

## Harness messages and results cleanup — 2026-10-27

- **431 core tests passed**, including Bash/Zsh and streaming/nonstreaming
  interactive acceptance. **23 Harbor adapter/reporting tests passed** against
  Harbor 0.25.0 and the current source runtime.
- Command results distinguish success, failure, timeout, and cancellation; report
  duration and configured timeout; and accurately identify reviewed commits.
  Empty output and preview/retention omissions are explicit without empty stream
  headings or zero-byte truncation disclaimers.
- Empty reads/listings/searches and terminal failure summaries are explicit.
  Headless outcomes add safe `message`/`next_step` fields while preserving status,
  reason, exit codes, and the distinction between completion and verification.
- Harbor reports include coverage, agent reasons, verifier rewards, manifest
  limits, and cost uncertainty. A new `summary.json` exposes the aggregate fields;
  `results.json` keeps the per-task format. Existing run artifacts were untouched.
- `git diff --check` passed. No paid evaluation or Docker rerun was performed.

## Harbor review Priority 1 fixes — 2026-10-27

- **428 core tests passed** with `.venv/bin/python -m pytest -q`.
- **28 optional Harbor tests passed** against Harbor 0.25.0 and the current source
  runtime. The local mock gateway required socket permission outside the sandbox.
- Unsupported tool arguments now return specific feedback and a usable correction
  when possible, without executing the rejected call or relaxing limits.
- Unmanaged patches now cross repository boundaries while preserving authorization,
  target checks, recovery, lint correction, and existing staging/commits. Managed
  cross-repository rejection remains covered.
- `git diff --check` passed. No paid benchmark or Docker rerun was performed.
  Remaining work is tracked in `integrations/harbor/benchmark/REVIEW.md`.

## Next-version M7 — Harbor integration complete

Local checks on 2026-10-27:

- **20 optional integration tests passed** against the actual **harbor==0.25.0**
  wheel, including its base class, auth resolver, agent factory, job/task models,
  and ATIF-v1.8 validator. The container boundary is mocked in these API tests.
- Success/failure/timeout runner subprocesses passed with closed stdin and mocked
  HTTP responses, using a fresh pinned Skua wheel installation outside the checkout
  import path. Native JSONL and ATIF were retained and validated. This is local
  process verification, not container verification.
- **411 core tests passed**; the optional dependency remains outside Skua runtime.
- Built and installed the optional `skua-harbor` wheel; both isolated environments
  passed `pip check`. Prepared smoke JobConfig and TaskConfig values passed the
  pinned models; Harbor's real CLI accepted the generated config with `--print-config`.
- `git diff --check` passed. README TOML examples still validate.

The tested Skua bundle wheel SHA-256 was
`9c6cc7e8762aef8b56d1c4cbe3868b5612998513031777cb3e8554601c876d2b`.
It was built from this checkout, not a released/pushed commit. Bundle manifests
record both the wheel and pinned runtime requirements hashes; future builds may
have different hashes. Harbor host dependencies are isolated, with Harbor pinned
exactly, but its transitive dependency graph is not a hashed lock.

The adapter uploads private task/config files, resolves the working directory,
passes credentials through environment execution fields, and installs a hash-checked
wheel into a dedicated container venv. Runtime settings use only Chat Completions,
disposable policy, managed Git off, and finite budgets. It collects native outcome
and history on nonzero exits; outer interruption gets an explicit missing-artifact
fallback when necessary. ATIF uses actual native tool-call/result references and
validated schema objects. Usage/cost remain unknown; no zero counts are invented.
Neither a completed turn nor an ATIF export is a verifier result.

**Container gate passed on 2026-10-27.** After the user installed Docker Desktop,
the three-case smoke ran against actual Harbor 0.25.0 and Docker Desktop 4.97.0 /
Engine 29.9.2 (Linux arm64 containers on macOS). It built the task image, installed
the hash-verified Skua wheel in an isolated container venv, executed each task,
and retained native and validated ATIF-v1.8 artifacts.

| Fixture | Trial ID | Skua status / exit code | External verifier |
| --- | --- | --- | --- |
| Success | `task-success__8Gs6G8F` | `completed` / 0 | Reward **1.0** |
| Command failure | `task-failure__jyzkguZ` | `failed` / 1 | Not run; expected agent error |
| Timeout | `task-timeout__W6B38qq` | `budget_exhausted` / 124 | Not run; expected agent error |

Harbor records RuntimeError for the deliberately failed/timed-out agent runs;
these are expected outcomes, not failed acceptance checks. The timeout transcript
retained the cancelled command's seven stdout bytes. Each trial's native history,
structured result, artifact hash manifest, and ATIF export were inspected. Usage
and cost fields remained null in Harbor's trial results. Uploaded input files were
removed, and no smoke-test containers remained running after completion.

Local artifacts are under `.skua/.tmp/harbor-docker-acceptance/jobs/`. The smoke
runner exited 0 with all three checks passed. Reproduction command used:

```sh
PATH="$HOME/.docker/bin:$PATH" PYTHONPATH=integrations/harbor LITELLM_LOCAL_MODEL_COST_MAP=True \
  .skua/.tmp/harbor-venv/bin/python integrations/harbor/smoke/run.py \
  --bundle .skua/.tmp/harbor-skua-bundle --output .skua/.tmp/harbor-docker-acceptance
```

Choose a new output directory when rerunning. No implementation fixes were needed
for this container run; prior 20 optional and 411 core test results above still
apply. M7 is complete. The manual GitHub Actions workflow has not been run or
inspected. These synthetic checks used no real provider or paid model and are not
an M8 benchmark result.

Reproduction instructions, optional host setup, the Docker command, and
`--prepare-only` are in the integration README. The fixture pins a Python image
tag rather than a digest; pin a tested digest before claiming an M8 reproducible
baseline. Keep any future CI result distinct from these local API tests.

## Next-version M6 — bounded headless execution complete

Local validation on 2026-10-27, macOS arm64 / Python 3.12.8:
**411 tests passed** with `.venv/bin/python -m pytest -q`; `git diff --check` passed.
Fresh offline pinned wheel installation passed with `pip check`, both entry
points, and interactive plus headless mocked acceptance under Bash/Zsh ×
streaming/nonstreaming. No runtime dependencies were added. No remote M6 CI run,
live-provider task, Harbor integration, or external task verifier was exercised.

Reproduce with:

```sh
.venv/bin/python -m pytest -q
.venv/bin/python scripts/verify_install.py --wheelhouse .skua/.tmp/milestone1-wheels
```

`tests/test_headless.py` supplies 32 completion checks. Real subprocesses run the
CLI without a TTY, with stdin closed or piped, and with HTTPX transport mocked
inside the child process (real sockets blocked). They verify completed, failed,
approval-required, timeout, SIGINT/SIGTERM cancellation, optional private artifact
files, no overwrites, empty input, invalid/unbounded limits, and API-credential
redaction. Numeric/escaped credentials cannot corrupt JSON syntax. Additional
shared-loop cases cover Git/non-Git edits, explicitly enabled managed commits,
failed-lint correction, detected sensitive access, errors, exact/cross-response
budgets, invalid calls consuming slots, pending calls marked not-run, pending
recovery blocking before credentials/endpoint access, retained command output,
model wait cancellation, and serializable history after unexpected failures.

Four additional shell/streaming tests run the same installed-package headless
scenario used by `scripts/installed_smoke.py`. Review policy returns
`approval_required` without writing; disposable policy writes the expected fixture
without terminal prompts, Git initialization, or transcript saves. Existing
interactive/cancellation/recovery/compaction regressions also pass.

The runner reuses the agent loop and runtime with an explicit total-call budget;
it does not silently approve interactive continuation prompts. The total deadline
includes model and tool execution after input is read, but waits for cleanup:
command termination and in-flight filesystem/Git mutations can extend wall time.
Managed Git subprocesses retain their existing 60-second timeout. This is not a
hard container-kill deadline. A final `completed` outcome is not task verification;
metadata always sets `task_success_verified` false and `provider_usage` null.

Policy is not isolation: disposable shell commands may access arbitrary files or
execute project code. Managed Git defaults off for headless runs independently of
interactive configuration. Recovery is never automatically restored or ignored.
Artifacts are explicit opt-ins, with outcome metadata excluding task/endpoint/
credential details; native transcript data may be sensitive and only the resolved
API credential is specifically redacted. Redacted identity fields can prevent
resuming an artifact. README.md documents CLI, exit codes, input/artifact contracts,
and the actionable `recovery_pending` workflow.

M6 is complete; M7 (pinned Harbor adapter and evaluation artifacts) is next and
has not started. Prior user-reported CI and M5 manual results below are historical
and must not be presented as remote validation of these M6 additions.

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

## M8 in progress — 2026-10-27

The configured U-M `gpt-5.6-luna` endpoint returned native tool calls and provider
usage. Terminal-Bench 2.0 is pinned to
`69671fbaac6d67a7ef0dfec016cc38a64ef7a77c` (89 tasks); task image digests,
Harbor 0.25.0, the Skua wheel hash, model settings, limits, and adapter source
hashes are recorded per phase under `.skua/.tmp/m8/`.

The first three lexicographically sorted tasks form the fixed pilot. The original
pilot encountered two missing-Python installation failures and one tool failure.
A hash-pinned private standalone Python runtime fixes the installation prerequisite
without changing system Python. The corrected pilot reached all three tasks:
one verifier pass (`bn-fit-modify`), one policy stop, and one verifier failure.
These are separate historical phases, not repeated attempts included in a score.
The full phase was stopped at the user’s request after 16 task outcomes; the
17th task, `crack-7z-hash`, was interrupted during environment startup. No full
baseline score is claimed. All benchmark processes stopped; no active-trial
container remained. Total accounted cost across all phases: $0.1347496, with
no pending usage reservations.

The user reported $19.42 in credits. Phase ceilings total $14.99: diagnostic $0.01,
original pilot $0.60, corrected pilot $0.14, full run $14.24. The host gateway
reserves conservative request costs before dispatch, records provider usage, and
retains reservations when usage is unknown. Real credentials stay on the host.
Amounts use published rates and are estimates, not independently verified billing.

Core regression: **411 passed** in 71.03 seconds. Optional integration regression:
**27 passed**, followed by **2 benchmark-report tests passed** after adding the
usage-summary check (28 optional tests in total). Coverage includes atomic cost
reservations, missing/over-bound usage, HTTP model/auth restrictions, SSE conversion,
standalone-runtime hashes/isolation, and failure/accounting report distinctions.

See `integrations/harbor/benchmark/README.md` for reproduction and limitations.
M8 remains incomplete. Observed blockers include missing Git in task images,
container policy denying legitimate external paths (including /dev/null), and
command path heuristics interpreting Python division as a filesystem path.
No fixes or additional evaluation runs were started after the stop request.

### M8 blocker fixes — 2026-10-27

Implemented after the user requested fixes, without resuming paid evaluation:

- Harbor installs missing Git with apt-get/apk, then checks Git, Bash, and
  sha256sum before contacting a model. Unsupported images fail during setup.
- Explicit `container` headless policy permits nonsensitive external task paths
  for reads, patches, and commands. Harbor selects it; `disposable`, interactive
  reviews, sensitive-path checks, and recovery handling retain their behavior.
- Python heredoc scanning examines quoted path literals instead of treating
  arithmetic punctuation as shell words. Real quoted paths and ordinary shell
  heredoc paths remain candidates. Detection remains heuristic, not isolation.

Validation: **415 core tests passed** (75.71s); **23 relevant optional adapter/
report tests passed**. The rebuilt installed-wheel Docker acceptance passed all
three cases: external-verifier success, expected command failure, and timeout
with retained artifacts. The success fixture deliberately starts without Git,
then exercises file discovery, reading `/etc/os-release`, Python division,
redirection to `/dev/null`, and patches inside `/workspace` and outside it under
`/tmp`. All five tool results were `ok`; verifier reward was 1.0. Trial:
`task-success__Lx6opNv`. Artifacts: `.skua/.tmp/harbor-fixed-acceptance/jobs/`.

Fixed core wheel SHA-256:
`56da6ed95670a7352461a3562457931c25cdba4445038bd3716288d298538486`.
No paid model calls were used. Old baseline results and ledgers are unchanged;
M8 remains incomplete until a separately requested corrected evaluation runs.

### Corrected benchmark restart — 2026-10-27

The user authorized restarting paid evaluation. Artifacts are separate under
`.skua/.tmp/m8-fixed/`. The fresh pilot completed with two agent timeouts and
one external verifier failure (0/3 passes). No approval denial or missing-Git
failure recurred. On `break-filter-js-from-html`, all five tool calls succeeded,
including file listing, reads, patching, and command execution; its native turn
completed but the verifier awarded 0. This confirms the integration fixes, not
solution correctness. The full 89-task phase is starting with unchanged limits
and a $14.24 phase ceiling. Including previous accounted spending and this
restart's phase ceilings, the maximum estimate is $14.5247496.

### Corrected run stopped and reviewed — 2026-10-27

Stopped at the user's request after 17/89 recorded outcomes: 4 verified passes,
6 agent timeouts, 3 unresolved-tool failures, 3 verifier failures, and 1 model
response failure. Task 18 (`custom-memory-heap-crash`) was interrupted during
startup; its leftover container and network were removed. Scheduler and Harbor
processes exited. No approval denials occurred in the completed sample.

Total accounted spending across both attempts, including pilots/diagnostics, is
$0.4665604; no pending reservations remained. This is a published-rate estimate,
not a billing statement. The partial sample is not a full baseline score.

Inspection found 15 unsupported-argument rejections across five tasks (11 timeout,
4 max_output_chars), one nested-repository patch rejection despite unmanaged Git,
missing-dependency output across eight tasks, and an incomplete model response
at the 8192-token output cap. Native outcomes also show incomplete execution/
verification and image-access limitations. See
`integrations/harbor/benchmark/REVIEW.md` for evidence, priorities, and acceptance
checks. No runtime fixes or paid reruns were performed during this inspection.
M8 remains incomplete and stopped. Prioritize validation feedback and unmanaged
patch independence, then environment/time feedback and a stronger pilot gate.
