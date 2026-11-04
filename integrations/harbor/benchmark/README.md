# Budgeted Terminal-Bench baseline

This local evaluation harness targets Terminal-Bench 2.0 at revision
`69671fbaac6d67a7ef0dfec016cc38a64ef7a77c` (89 tasks), Harbor 0.25.0,
and the configured U-M `gpt-5.6-luna` Chat Completions model. It is a constrained
baseline, not a standard leaderboard submission. The default `--subset reviewed-text`
selects 49 tasks after visual and documented-issue exclusions. `--subset text-only`
restores the original 82-task visual-only filter; `--subset all` restores all 89.
The fixed capability pilot is `modernize-scientific-stack` (dependency modernization),
`pypi-server` (package service), and `pytorch-model-cli` (inference and saved output).
These are chosen from public requirements, not verifier scores. The first two replace
`build-cython-ext` and `configure-git-webserver`, which have documented task defects.

## Reviewed issue exclusions

[task-exclusions.json](task-exclusions.json) records 36 reported problematic tasks,
with reasons, evidence levels, sources, review date (2026-10-28), and dataset revision.
Three overlap the seven visual exclusions, leaving 49 eligible tasks. The conservative
filter covers instruction/verifier mismatches, dependency/resource failures, and
integrity defects that can inflate scores as well as reject correct answers.
Sources include the [upstream 2.0 correction review](https://github.com/harbor-framework/terminal-bench-2/pull/53),
individual upstream issues, and the [Fidian task audit](https://fidian.ai/blog/did-your-agent-really-improve/).
Only the Git webserver authentication failure was reproduced locally during this review;
other entries are reported issues, not a claim that every environment reproduces them.
Remaining tasks are not certified defect-free. Ordinary model failures, budget stops,
reference-solution-only bugs, and 2.1-specific reports are not sufficient to exclude a task.

The manifest and JSON summary preserve the complete policy and its SHA-256; Markdown
reports link the evidence. A changed policy requires a fresh matching pilot. Existing
results and ledgers remain historical records, including the failed Git verification.

## Text-only selection

The pinned exclusion list was reviewed against public task instructions, without
reading solutions or verifier tests. It excludes tasks whose objective requires
recovering content or scene meaning from visual inputs, including rendered geometry:

| Excluded task | Visual requirement |
| --- | --- |
| `chess-best-move` | Read a chess position from a PNG |
| `code-from-image` | Read pseudocode from an image |
| `extract-moves-from-video` | Transcribe commands from gameplay footage |
| `financial-document-processor` | Read/classify JPG and PDF documents |
| `gcode-to-text` | Recognize text formed by G-code toolpaths |
| `path-tracing` | Reconstruct a scene from a reference image |
| `video-processing` | Infer jump events from footage |

The `text-only` option is a curated capability subset, not an official Terminal-Bench category or
a promise that every remaining task is solvable by Skua. Text-specified programming
work remains included under that option even when it processes images: `caffe-cifar-10`,
`pytorch-model-cli`, `sam-cell-seg`, and `build-pov-ray`. Disk/container images are
unrelated to visual input and do not trigger exclusion. Selection uses explicit
task names, not keyword matching, file extensions, or prior verifier outcomes.

Preview selection without a bundle, credentials, Docker, or model requests:

```sh
PYTHONPATH=integrations/harbor python integrations/harbor/benchmark/run.py \
  --phase full --data DATA --subset reviewed-text --list-tasks
```

Use the optional Harbor Python environment. Preview validates the pinned archive
and dataset, prints selected task names and exclusion reasons as JSON, and creates
no run directory. The manifest and reports retain the subset, original dataset
count, eligible count, and exclusions separately from selected-but-unrecorded tasks.
Subset results must not be reported as a full 89-task benchmark score.

Pilot and full runs must use the same subset and exclusion list; historical pilot
manifests without a subset are treated as `all`. Use a fresh data directory for a
new evaluation, preserving all historical ledgers. The full-run allowance remains
$0.28 per reviewed-text task; further exclusions do not redistribute allowances (49 tasks
account for at most $13.72). The original unfiltered cohorts retain $0.16 per task,
subject to the $13.72 phase cap.

By default each task gets 120 seconds and 40 tool calls; each request gets at most
8192 completion tokens. An explicit `--max-seconds` selects a different task time
ceiling and records it in the manifest. Harbor allows that ceiling plus 90 seconds
for agent execution/cleanup, 600 seconds for installation, and 300 seconds for verification.
There is one attempt per phase, no Harbor retries, and no temperature/seed override.
Time changes do not change dollar caps. Use a new run directory and inspect a small
pilot before any full run; full evaluation requires a pilot with the same time
ceiling, task subset, bundle hashes, harness Python source hashes, model, endpoint,
platform, tool-call ceiling, and completion-token cap. All three pilot tasks must
have recorded outcomes, with no infrastructure, policy, model, interruption, or
cost-limit category, and at least one externally verified pass. This is a minimum
readiness gate, not evidence of representative benchmark performance. No paid
pilot has been run with the current changes.

The native runner now supplies PATH-based environment hints and remaining-time
feedback. Its private Skua Python is explicitly distinguished from task Python;
discovery does not execute programs or preinstall task dependencies.
Linux amd64 task images run under Docker Desktop emulation on this Apple Silicon
host. Each image is resolved to its digest before execution. New task images are
removed afterward; pre-existing images are retained. A low host disk check stops
new tasks below 5 GiB free. Task instruction, solution, and verifier contents are
not read by the harness: Harbor supplies instructions and stages verification.

## Spending controls

The historical user-supplied budget was $19.42 remaining credit. The phase ceilings
are $0.01 connectivity, $0.60 original pilot, $0.48 runtime-corrected pilot, and
$13.72 full evaluation: **$14.81 total** if all four phases are run. The runtime
pilot allows $0.16 per task. These increased per-task allowances were authorized
on 2026-10-28 after selecting the 49-task cohort. These figures
do not establish current remaining credit or authorize another spend. A fresh
reviewed-text diagnostic, runtime pilot, and full run have combined route allowances
of $0.01 + $0.48 + $13.72 = **$14.21**. These are estimated controls, not a guarantee
of the provider's invoice or remaining quota.

The gateway is intentionally restricted to this model and the
[U-M published rates](https://www.its.umich.edu/computing/ai/pricing): $0.20/M input,
$1.20/M output tokens, without cache discounts. It durably reserves a conservative
input byte bound plus maximum completion cost before each request, then refunds
the difference when valid usage is returned. Unknown/error usage keeps the full
reservation. Reported usage above the bound halts further calls. Concurrent requests
share an atomic ledger; each task has its own ceiling as well. A new invocation
refuses an existing phase directory: never delete ledgers to reset spending.
Re-running with a new data directory spends additional money and requires a new
budget decision. There is no cross-directory or provider-wide quota enforcement.

The real credential is resolved on the host using normal Skua configuration. Only
a random, model/budget-restricted gateway token reaches Harbor and containers.
The gateway binds a host port for Docker access and requires that token. Upstream
requests are buffered nonstreaming to obtain usage; downstream responses use SSE.
This changes streaming latency and is recorded in the manifest. Non-text message
content is rejected before budget reservation. Ledger files store request hashes,
usage, and allowlisted finish reasons, not prompts or credentials. Private native/ATIF artifacts
may still contain task data; keep the output directory ignored.

## Reproduction

Use the optional Harbor environment from the parent README and a new ignored data
directory. Fetch the official Harbor registry entry for `terminal-bench@2.0` as
`dataset.json`, then download its repository archive at the revision above as
`dataset.tar.gz`. The required SHA-256 is
`afcd2b9813fc08e8d2b955db8165cc057acf0e37f74e55d26fc2506da196cebe`.
Extract it into `source/terminal-bench-2-<revision>/` without modifying the source.
The harness changes only copied task image references to immutable digests.

Some task images have no Python. Supply the trusted standalone archive
`cpython-3.12.12+20251014-x86_64-unknown-linux-gnu-install_only.tar.gz` from
[python-build-standalone release 20251014](https://github.com/astral-sh/python-build-standalone/releases/tag/20251014).
Verify SHA-256 `1ab2b6594d1c3d76cbebea09d6bc3e6ba68d8eb3b6322080375c4cc3dd188f34`.
Build with `build_bundle.py --output BUNDLE --python-archive ARCHIVE`. The adapter
checks the archive hash before upload and inside the container, extracts it under
`/installed-agent/skua`, and uses its Python for a private venv. It does not replace
the task's system Python or add this runtime to task commands' PATH. This archive
requires Linux amd64/glibc; incompatible environments remain infrastructure failures.

With `docker` on PATH, run from the repository root (the Python executable must be
in the optional Harbor venv):

```sh
export PYTHONPATH=integrations/harbor
export LITELLM_LOCAL_MODEL_COST_MAP=True
python integrations/harbor/benchmark/run.py --phase diagnostic --data DATA --bundle BUNDLE --subset reviewed-text
python integrations/harbor/benchmark/run.py --phase pilot-runtime --data DATA --bundle BUNDLE --subset reviewed-text
python integrations/harbor/benchmark/run.py --phase full --data DATA --bundle BUNDLE --subset reviewed-text
```

The historical `pilot` phase documents installation failures before the private
runtime was added; it need not be repeated. Full evaluation requires a successful
connectivity check and a matching three-task pilot meeting the gate described above.
Each phase writes a manifest, harness source snapshot, budget ledger, per-task
Harbor configs/logs, native outcomes/trajectories, `results.json`, `summary.json`,
and `report.md`. The summary separates recorded and unrecorded tasks, verified
passes, accounted budget, estimated cost from reported usage, and requests with
unknown usage. Reports include agent reasons and verifier rewards, and read
execution limits from the run manifest. Historical run artifacts are not rewritten.
Secondary flags retain observed invalid arguments, missing-dependency messages,
repository restrictions, explicit refusals, response truncation, and content filtering.
Flags describe observations during a task; they do not prove an error remained
unresolved or change the external verifier's result.
The wheel hash identifies the evaluated core even when Git has uncommitted changes.
Native provider usage remains null; the separate gateway ledger is authoritative
for observed evaluation usage. A completed Skua turn is not a passing task: only
external verifier reward 1 counts as a pass. Unverified attempts remain unsuccessful
and are categorized separately. Consult the report's coverage before interpreting
its attempted-task denominator; incomplete runs are not full-dataset scores.

SIGINT/SIGTERM stops scheduling new tasks, sends SIGTERM to the active Harbor
process, and allows up to 60 seconds for its cleanup before killing the child
process group. The active task is recorded as `interrupted`; manifests and reports
retain the stop reason and exit status is 128 plus the signal number. Cleanup
checks exact Docker Compose project labels derived from this run's recorded trial
identity, removing only its containers, networks, and volumes. Cleanup failures
are retained in the task result. An in-progress setup/metadata command can finish
its bounded timeout before interruption is handled; unknown upstream usage keeps
its budget reservation. SIGKILL and host power loss cannot run cleanup handlers.

Before a paid pilot, run the synthetic container checks and signal check against
the freshly rebuilt bundle (these use only an in-container mock endpoint):

```sh
DOCKER_DEFAULT_PLATFORM=linux/amd64 python integrations/harbor/smoke/run.py \
  --bundle BUNDLE --output NEW_SMOKE_DIRECTORY
DOCKER_DEFAULT_PLATFORM=linux/amd64 python integrations/harbor/smoke/interrupt.py \
  --bundle BUNDLE --output NEW_INTERRUPT_DIRECTORY
```

## Post-baseline fixes

The stopped baseline used project-scoped `disposable` policy and did not install
Git into images lacking it. The adapter now installs/checks Git before contacting
a model and selects explicit `container` policy for external task paths. Python
heredoc path detection no longer treats division as a shell path. These changes
were tested with a mock-model Docker fixture; the old baseline artifacts remain
unchanged. Rebuild the Skua bundle before any future evaluation. No paid rerun
is implied by these fixes.

## Verification after ordinary tool failures

An agent turn ending with `unresolved_tool_error` (exit 1) now returns normally
from the Harbor adapter so the external verifier can assess existing task files.
Native status, reason, exit code, and trajectory remain unchanged. Policy denials,
interruptions, budget stops, other execution failures, and invalid trajectories
still fail the adapter; this change does not authorize blocked operations.

Outcome categories describe execution failures, while `verified_passes` counts
actual reward=1 results independently. Thus a `tool` category may have reward 1,
0, or no reward; missing verification never becomes a pass. JSON summaries also
record verified/unverified task counts. Historical run files are not rewritten.
The Docker smoke suite covers valid outputs followed by a failing command, and
an ordinary tool failure with no valid output, using a synthetic endpoint.

Optional native result files are parsed independently from Harbor trial results.
Missing, empty, unreadable, malformed, or non-object outcomes produce secondary
flags instead of discarding the trial path, Harbor exception, or verifier reward.
Valid transcript events and gateway signals survive malformed transcript lines.
Missing or ambiguous Harbor trials remain infrastructure outcomes; the parser
never selects an arbitrary trial or invents verification results.

## Git bootstrap for pinned QEMU images

The two recorded QEMU image digests use the explicit `bullseye-20251020` Git
bootstrap profile. Their live Bullseye security indexes referenced packages that
returned 404 even after fresh HTTP/HTTPS index downloads and a security-origin
check. Installation succeeds using the 2025-10-20 Debian snapshot date already
recorded in the image's source comments.

Only bootstrap uses temporary snapshot sources and separate indexes; task APT
sources are not edited. The snapshot retains signature/package-hash verification;
`check-valid-until=no` applies only to those dated snapshot source entries, whose
metadata is intentionally historical. Temporary indexes are removed on exit.
Git is installed only if absent; existing Git and all other image digests retain
the default path. The profile is recorded in job configuration, image metadata,
and the installed artifact record. Changing an image digest does not silently
reuse this exception. Package installation errors now stop setup immediately.

Validated on both exact Linux amd64 images using disposable containers: Git
2.30.2 installation, basic repository operations, unchanged task source-file
hash, temporary-index cleanup, and an already-installed Git check. No model
requests or benchmark task-solving retries were used. This repairs agent setup,
not the tasks' external verifier or any package installations they may require.

## Focused diagnostic reruns

Use `--phase rerun --tasks NAME ...` with a fresh data directory and successful
connectivity diagnostic to repeat explicit eligible tasks. Each task retains the
full-phase per-task allowance; the phase cap equals selected count times that
allowance. The full-run pilot gate is unchanged. Focused reruns are diagnostic,
selection-biased attempts, not a replacement full benchmark score; preserve the
original run separately. All existing bundle, archive, policy and image pins,
source snapshots, execution limits and immutable phase ledgers still apply.
