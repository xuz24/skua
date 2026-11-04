# Skua × Harbor (M7)

Optional integration pinned to **Harbor 0.25.0** and its **ATIF-v1.8** models.
The adapter subclasses the installed package's `BaseInstalledAgent`; Harbor stays
out of the normal Skua wheel and runtime dependencies.

**Status:** M7 is complete. The real Docker/Harbor success, failure, and timeout
trials passed locally on 2026-10-27, including the success task's external verifier
and retained schema-valid artifacts for all three outcomes. See
[validation evidence](../../VALIDATION.md). No benchmark score or paid model
evaluation is claimed.

## Install the separate host environment

From the repository root, with the normal Skua development `.venv` already set up:

```sh
python3.12 -m venv .skua/.tmp/harbor-host
.skua/.tmp/harbor-host/bin/python -m pip install -r integrations/harbor/requirements.txt
.skua/.tmp/harbor-host/bin/python -m pip install --no-deps -e integrations/harbor
LITELLM_LOCAL_MODEL_COST_MAP=True .skua/.tmp/harbor-host/bin/python -m pytest -q integrations/harbor/tests
```

Harbor is pinned exactly; its transitive host dependencies are not a hashed lock.
They are isolated from Skua's pinned runtime dependencies. The environment variable
above selects LiteLLM's bundled cost map during synthetic testing, avoiding its
optional online lookup. It does not manufacture usage or cost data.

## Build the Skua artifact

```sh
.venv/bin/python integrations/harbor/build_bundle.py --output .skua/.tmp/harbor-bundle
```

Use a new output directory for each build. This builds from the current checkout,
including uncommitted code, rather than assuming a commit or fetching `latest`.
The manifest pins the wheel and runtime requirements file by SHA-256. Keep that
manifest with the resulting trial. The adapter rechecks hashes before upload and
inside the container before installation; it uses a dedicated
`/installed-agent/skua/venv` and installs the wheel with `--no-deps` after the pinned
requirements. No mutable source checkout is installed in the task container.

Task images must provide Bash and `sha256sum`, plus Python 3.12/venv unless the
bundle includes the optional standalone Python archive (see the benchmark README).
The adapter installs missing Git and CA certificates using apt-get or apk, then
checks Git/Bash/sha256sum before any model request. Images without a supported
package manager must supply Git themselves; setup fails otherwise. Git is needed
for discovery and patches even when managed commits are off. Installation needs
package repository/index access; Git's installed version is retained in setup logs.
The smoke image deliberately lacks Git and exercises this bootstrap path. Setup runs as root; task execution uses Harbor's
configured agent user and the container's working directory.

## Container acceptance (requires Docker)

Keep Docker Desktop running. If a terminal session predates its installation,
add the CLI directory to that session's PATH and confirm the server is reachable:

```sh
export PATH="$HOME/.docker/bin:$PATH"
docker version
```

```sh
LITELLM_LOCAL_MODEL_COST_MAP=True .skua/.tmp/harbor-host/bin/python integrations/harbor/smoke/run.py \
  --bundle .skua/.tmp/harbor-bundle --output .skua/.tmp/harbor-smoke
```

This invokes actual Harbor Docker trials for success, command failure with reward 0,
valid output followed by a command failure with reward 1, and an
internal execution timeout. A task-local synthetic Chat Completions server supplies
responses, so no API key or paid model is used. The success trial must also pass
Harbor's external file verifier; normal agent completion alone is insufficient.
Failure and timeout trials must retain native history, structured outcomes, and
schema-valid ATIF. Inputs and verifier files are separate; the verifier is not
copied into the agent image.

Without Docker, add `--prepare-only` to create configurations and validate them
against the pinned API. That is not a container test. The fixture uses the versioned
`python:3.12.12-slim-bookworm` tag, not an immutable image digest; pin a tested digest
before a reproducible benchmark baseline.

Alternatively, after pushing these files, manually run the **Harbor integration
smoke** GitHub Actions workflow. It performs the same synthetic container checks
on Ubuntu and retains artifacts. It is manual-only and has not been run here.

## Use the adapter with an actual task

Example Harbor job configuration (replace paths and choose a model deliberately):

```json
{
  "job_name": "skua-task",
  "n_concurrent_trials": 1,
  "agents": [{
    "import_path": "skua_harbor.agent:SkuaAgent",
    "model_name": "provider/model-id",
    "base_url": "https://your-chat-completions-endpoint.example/v1",
    "model_api_key_name": "SKUA_API_KEY",
    "model_api_format": "openai_chat_completions",
    "kwargs": {
      "bundle_path": "/absolute/path/to/harbor-bundle",
      "max_seconds": 300,
      "max_tool_calls": 100
    }
  }],
  "tasks": [{"path": "/absolute/path/to/task"}],
  "environment": {"type": "docker"}
}
```

Use `harbor run --config JOB.json` in the optional host environment. Put credentials
in the named environment variable, never in job JSON, task instructions, or command
arguments. Harbor resolves provider prefixes and the selected credential; the
adapter passes the resulting model ID unchanged to Skua and maps the key to
`SKUA_API_KEY` in the environment execution call. Set the AgentConfig field
`skip_model_parsing: true` for literal model IDs containing slashes. Only Chat
Completions is supported; Responses, Anthropic Messages, and other native protocols
are rejected. Supply an explicit base URL, including any required `/v1` prefix.
For a deliberately unauthenticated endpoint use `auth_enabled: false` in kwargs.

Task text is uploaded as a private UTF-8 file, never interpolated into a shell
command. Endpoint/model settings use a separate private JSON file, with no key.
Both inputs are deleted after execution when the container is reachable. The
runner constructs explicit Skua configuration so a task-local config cannot change
the selected endpoint or enable managed Git. It uses the explicit `container` unattended
policy, managed Git off, and finite execution budgets. Container policy permits
nonsensitive paths outside the working directory, including `/tmp` task files
and `/dev/null`; sensitive-path checks remain enabled. Policy is not a sandbox;
Harbor's environment supplies isolation. MCP, skills, and resume/load are unsupported.

Set the task's outer agent timeout above `max_seconds + 90` to allow Skua cleanup
and artifact transfer. An outer container interruption can prevent native history
from being finalized; the adapter then records missing artifacts rather than
inventing a trajectory. A nonzero Skua outcome raises a Harbor agent error after
collection, so an error is not silently presented as success.

## Artifacts and limits

Harbor's agent log directory contains:

- `artifact.json`: Harbor/adapter versions and exact wheel/requirements hashes.
- `result.json`: Skua's structured outcome, or an explicit missing-artifact fallback.
- `session.jsonl`: native events, including approvals, tool results, and cancellation.
- `trajectory.json`: ATIF validated with Harbor 0.25.0's `Trajectory` model.
- `trajectory-error.json`: fixed diagnostic if native history cannot be converted.

The exporter maps assistant tool calls and matching observations by native sequence
number. It preserves interrupted message and tool statuses, and turn outcomes.
It does not invent hidden reasoning, costs, or token counts: Skua currently exposes
no provider usage, so Harbor context fields stay null and ATIF metrics are omitted.
Approval details and notices remain in native history instead of being mislabeled
as model calls. A trajectory is not proof that the external verifier passed.

Native transcripts can contain task or tool secrets; Skua redacts the resolved API
key, not arbitrary sensitive data. Redaction can alter identity fields, so native
artifacts are not guaranteed resumable. Inputs and exported trajectories should
be treated as private. The synthetic CI upload contains fixtures only and needs
no repository API secrets.

## Primary references

- [Harbor 0.25.0 release](https://pypi.org/project/harbor/0.25.0/)
- [Custom installed agents](https://docs.harborframework.com/agents/custom-agents)
- [Harbor ATIF specification](https://github.com/harbor-framework/harbor/blob/main/rfcs/0001-trajectory-format.md)

The documentation can evolve; tests target the actual 0.25.0 wheel's base class,
auth resolver, factory, job/task models, and ATIF validator.

## Terminal-Bench evaluation

See [the budgeted baseline harness](benchmark/README.md) for the pinned M8 dataset,
model, cost controls, private Python runtime option, and real-run artifacts.
Synthetic smoke acceptance alone is not a benchmark score.

## Task-generated certificate keys

The trusted Harbor runner explicitly allows new `.key` paths under the task
working directory's `ssl/` and `/etc/nginx/ssl`. The first approved operation must
reference an absent key path; later operations in that run may use that admitted
path. Pre-existing keys, paths elsewhere, symlinked paths, other sensitive names,
and configured sensitive matches remain denied. `settings.json`/`task.txt` input filenames remain sensitive. Runtime binaries
under `/installed-agent` are not categorically sensitive: task validation may
execute the bundled Python when no task interpreter is available. Built-in
credential-path restrictions remain in effect; arbitrary shell execution is
not an isolation boundary.
The opt-in is internal to the adapter, not configurable through task instructions
or task-local settings, and requires container policy. Normal container sessions
retain their default sensitive-file restrictions.

This is an authorization heuristic for disposable benchmark containers, not a
shell sandbox: it does not parse shell write intent or guarantee prevention of
computed-path/environment access. Real provider secrets must continue to use the
existing host gateway; only its limited token is supplied to the container.
New bundles are required for this core-policy change. Historical runs and pinned
bundles are unchanged. The synthetic Docker suite includes creating keys in both
allowed directories; headless tests cover denied pre-existing/out-of-scope keys,
symlinks, mixed sensitive operations, and configured sensitive paths.

## QEMU host compatibility

The two pinned QEMU task images now opt into a short runtime check during adapter
setup, before any model request. It launches the image's unmodified QEMU without
a guest and requires it to stay alive until the probe timeout. A Rosetta syscall
failure stops setup with an actionable message instead of consuming task calls.

On this Apple Silicon host, disabling Docker Desktop's “Use Rosetta for
x86_64/amd64 emulation” setting and restarting Docker resolved syscall 282.
This changes host-wide amd64 emulation and may reduce performance. Native amd64
hosts need no such change. The task images, QEMU binaries, budgets and verifiers
are unchanged. See [Docker settings](https://docs.docker.com/desktop/settings-and-maintenance/settings/)
and the [upstream emulation report](https://github.com/docker/for-mac/issues/7475).
