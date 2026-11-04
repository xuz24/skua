# Stopped M8 evaluation: findings and next work

## Implementation follow-up — 2026-10-27

Both Priority 1 fixes below are implemented. Unsupported top-level arguments now
identify rejected/missing fields, list supported fields, state that execution did
not occur, and offer a valid corrected call when possible. Strict validation is
retained. The command description explains timeout ownership/seconds and output
limits in bytes; no per-call overrides were introduced.

Unmanaged patches can span launch-root, nested, and external repositories. Managed
mode still rejects cross-repository patches. Existing authorization, file identity
checks, launch-repository identity checks, recovery, and lint correction remain.
Regression tests cover staging/commit preservation, external authorization,
target replacement/symlinks, rollback, cross-repository lint correction, repeated
invalid-call recovery, and shared time/call/output limits.

Validation: **428 core tests passed**, **28 optional Harbor tests passed**, and
`git diff --check` passed. The optional gateway test required localhost socket
permission outside the sandbox. No paid evaluation or Docker rerun was performed.
The Priority 2 environment/time-context implementation is described below; its
live pilot remains pending. Execution/verification guidance and refusal diagnostics
are also implemented with offline fixtures; live effectiveness remains pending.
Priority 3 and other evaluation-harness improvements remain pending.
M8 remains stopped and incomplete.
The findings below describe the original run evidence.

## Original inspection

Inspected 2026-10-27 after the user requested stopping the corrected run.
The scheduler and Harbor processes exited; the interrupted task's container and
network were removed. No implementation changes or further paid runs were made
as part of this inspection. Original evidence remains unchanged except stop metadata.

## Coverage and cost

The corrected full phase recorded **17 of 89 task outcomes** before interruption
of task 18 (`custom-memory-heap-crash`) during environment startup:

| Outcome | Count |
|---|---:|
| External verifier passed | 4 |
| Agent time limit | 6 |
| Unresolved tool error | 3 |
| Completed turn, verifier failed | 3 |
| Model response failure | 1 |
| Policy denial / infrastructure failure among completed attempts | 0 |

Verified passes: `build-pmars`, `cancel-async-tasks`, `cobol-modernization`, and
`constraints-scheduling`. This is a partial lexicographic sample, not a full
benchmark score or a representative estimate. Tasks may have multiple underlying
problems; the primary outcome label hides some of them.

Total accounted cost across both attempts and their pilots/diagnostics:
**$0.4665604** ($0.1347496 original + $0.3318108 corrected). These are estimates at
published rates, not an invoice. No pending reservations remained at inspection.
The corrected full phase had 163 gateway requests with reported usage and zero
budget denials. Cost was not the reason these tasks stopped.

The prior Git/bootstrap and project-scope permission fixes worked in this sample:
no tool result was denied, and file discovery and patches succeeded in multiple
verified tasks. Remaining defects should be addressed separately.

## Priority 1: actionable tool validation and correction

**Evidence:** 15 rejected calls across five tasks supplied unsupported arguments:
11 `timeout` arguments and four `max_output_chars` arguments. `run_command` accepts
only `command`. The response says only “Missing required arguments or unknown
argument names.” The model repeatedly retried unsupported fields, including on
commands intended to install dependencies. These counts include failures in tasks
whose primary outcome was not `tool`.

**Change:** identify the rejected field, list supported fields, explicitly state
that the command did not execute, and return an immediately usable correction.
Document timeout units and ownership in the tool description. Do not silently
ignore an unsupported timeout, reinterpret milliseconds as seconds, or let a tool
argument extend the overall deadline. If per-command timeout/output controls are
added, give them explicit schemas, bounds, and effective-limit feedback.

**Acceptance:** replay invalid calls with `timeout` and `max_output_chars`; confirm
zero side effects, specific feedback, a successful corrected call, and preserved
total time/output limits. Test repeated invalid-call recovery without disabling
strict argument validation.

## Priority 1: remove managed-Git restrictions from unmanaged patches

**Evidence:** `build-cython-ext` tried to patch files under `/app/pyknotid` and got
“Split patches by repository.” In `skua/workflows.py`, `repo_for()` compares target
repositories with the launch-root repository even when managed Git is disabled.
This is an actual Skua workflow restriction, not a compiler or model failure.

**Change:** make unmanaged patches independent of repository boundaries while
retaining per-target authorization, identity checks, recovery, and lint behavior.
Keep repository/checkpoint restrictions for managed workflows. Do not work around
this by instructing the model to abandon the patch tool for shell writes.

**Acceptance:** unmanaged edits to nested and external repositories succeed with
appropriate authorization and preserve all staging, commits, and unrelated files;
managed mode still rejects unsupported cross-repository operations. Test recovery
and target replacement/symlink checks in both cases.

## Priority 2: environment discovery and useful time budgets

**Implementation update:** unattended planning calls now receive refreshed PATH
discovery (interpreters, package managers, build tools), cwd/configured shell and
environment, separate agent-Python identity, remaining time/calls, and output limits.
Discovery never invokes programs or activation scripts and labels its uncertainty.
Command dispatch records its effective time ceiling; tool results return remaining
time. Both use the existing shared deadline, including task prerequisite setup,
checks, and model work (not Harbor's separate agent-installation phase).
Offline fixtures cover missing prerequisites, setup, verification, PATH refresh,
and deadline/call-limit preservation. The benchmark accepts explicit `--max-seconds`
with unchanged dollar caps and matching pilot/full time settings. A longer paid
pilot and native-amd64 comparison have **not** been run; effectiveness on real tasks
is still unverified.

**Evidence:** eight tasks produced 17 command results reporting missing executables
or Python modules. Examples include `python`, pandas, `make`, `gcc`, Pillow, and
setuptools. These are task-environment dependencies, distinct from Skua's private
runtime. Installing Git does not supply a build toolchain. Six tasks hit the
120-second overall deadline. R installation, Caffe dependency installation, Python
package installation, and password search were still running when cancelled.

**Change:** provide concise runtime context (cwd, available interpreters, package
manager, effective tool limits, remaining task time), and encourage capability
checks before assuming packages exist. Make the distinction between the private
agent Python and task Python explicit. Surface remaining time before expensive
commands. Test a longer, explicitly selected task-time ceiling in a small pilot;
retain the dollar cap. Consider native amd64 execution to remove emulation as a
confounder, but do not assert its quantitative impact without measurement.

Do not preinstall task-specific answers/dependencies or silently alter benchmark
images. Dependency installation is often part of solving the task. Any generic
runtime additions or changed limits must be declared as a new run configuration.

**Acceptance:** mocked tasks discover and install missing prerequisites, then
continue; deadlines are visible and honored. A small declared pilot demonstrates
whether extra time yields verified solutions before another full run is launched.

## Priority 2: execute and verify the requested outcome

**Implementation update:** unattended runtime context now explicitly directs the
agent to perform the requested outcome, reserve budget for an observable check,
repair failed checks when permitted, and report incomplete or unverified work.
Advice/code-only requests retain their scope. Offline scripted-model fixtures
distinguish setup-file creation from applied configuration and a saved answer from
a successful, checked calculation. They exercise real tools but do not measure
live model compliance. Provider-supplied refusal fields now survive response
assembly as a `model_refusal` transcript notice and result diagnostic, without a
correction retry or a transport-failure classification. Text-only refusals are not
heuristically classified. External task success remains unverified by the harness;
no paid pilot has been run for this change.

**Evidence:** `configure-git-webserver` produced a setup script and instructions
for the user to run it, then failed external verification. `count-dataset-tokens`
encountered a calculation SyntaxError and subsequently wrote an answer file; the
verifier rejected the result. The transcript does not establish a successful final
calculation. `break-filter-js-from-html` returned a refusal without tool calls.
These are different causes despite all receiving the primary `task` label.

**Change:** make the unattended objective explicit: perform the requested work and
check observable results before concluding, rather than hand off installation
instructions or unsupported answers. Record refusals as a separate diagnostic
without treating them as transport failures or automatically trying to evade them.

**Acceptance:** fixtures distinguish writing a setup script from actually configuring
a service, and distinguish writing an answer from validating its computation.
Completion text alone must never imply verifier success.

## Priority 3: image access and bounded incomplete-response handling

**Evidence:** image tasks attempted pixel/OCR work through shell commands because
Skua exposes no image tool. `code-from-image` also repeatedly invented an output
limit argument. On `chess-best-move`, the final gateway request reported exactly
8192 completion tokens (the configured cap), followed by `incomplete_response`.
Output truncation is the likely cause, but the gateway did not retain finish_reason;
the client currently maps both length and content filtering to that generic error.

**Change:** record safe finish-reason metadata and distinguish output limits,
content filtering, and transport interruption. Never execute incomplete tool calls.
Evaluate bounded recovery with an explicit continuation budget. Investigate an
image-reading path only after verifying provider/model capability; current
byte-based gateway input reservations are text-only and must be revised before
allowing image requests. Do not merely raise output caps globally.

**Acceptance:** mocked capped responses preserve diagnostic reasons and partial
text without executing partial tools; retries consume the shared budgets. Image
support, if selected, has explicit usage accounting and appropriate capability tests.

## Evaluation-harness improvements before another full run

**Readiness update — 2026-10-27:** signal handling now stops scheduling, records
the interrupted trial, bounds Harbor termination, and cleans up resources using
the exact recorded trial's Docker Compose labels. Synthetic SIGTERM container
acceptance confirmed no owned containers, networks, or volumes remained. The
pilot now covers nested/dependency builds, service setup, and inference output;
the full gate requires all three outcomes, at least one verified pass, no listed
infrastructure/policy/model/cost failures, and matching build/configuration hashes.
The gateway records safe finish reasons and rejects non-text inputs before cost
reservation; reports retain secondary diagnostic flags. These changes are tested
offline and with mock endpoints. A fresh paid matching pilot remains required;
the old pilot cannot unlock the current full run. Bounded model continuation
recovery remains deferred; incomplete tool calls are still never executed.

- Add graceful signal handling that stops scheduling, terminates the active trial,
  retains an interrupted outcome, and cleans up container/network resources.
  Both stops required manual host-process handling; this stop left a container.
- Record secondary failure flags alongside the primary outcome: invalid arguments,
  missing dependencies, patch restrictions, refusal, and response truncation.
- Gate the full phase on concrete capability checks, not merely “not all pilot
  tasks failed infrastructure.” The corrected pilot's 0/3 score was disclosed,
  but that gate was too weak to justify another long run.
- Use task metadata and capability diversity to choose a fixed pilot before seeing
  scores; include nested-repository edits and dependency setup. Preserve task
  solution/verifier separation. Keep historical attempts distinct and retain ledgers.

Evidence: `.skua/.tmp/m8-fixed/full/{results.json,budget.json,manifest.json}` and
per-task native `agent/session.jsonl` / `agent/result.json` under `full/jobs/`.
Only native agent evidence and verifier rewards were inspected; hidden verifier
and reference-solution contents were not read. M8 remains incomplete and stopped.
