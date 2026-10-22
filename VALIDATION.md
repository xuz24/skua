# Validation and release evidence

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
7. Export Markdown, decline remote fetch, explicitly choose reversing undo twice,
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
