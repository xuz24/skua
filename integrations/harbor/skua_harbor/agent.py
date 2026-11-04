"""Pinned Harbor installed-agent adapter. No Harbor imports enter core Skua."""
import hashlib
from importlib.metadata import version
import json
import math
from pathlib import Path, PurePosixPath
import re
import shlex
import tempfile

from harbor.agents.agent_auth_resolution import ModelConnectionSpec, ProviderAccess
from harbor.agents.capabilities import AgentCapabilities
from harbor.agents.installed.base import BaseInstalledAgent
from harbor.agents.options import InstalledAgentOptions
from pydantic import Field
from harbor.models.model_api_format import ModelApiFormat

from . import ADAPTER_VERSION, HARBOR_VERSION
from .atif import export_trajectory
from .bootstrap import GitBootstrap, git_bootstrap_command, QEMU_RUNTIME_CHECK

INSTALL = PurePosixPath('/installed-agent/skua')


def checked_bundle(directory):
    directory = Path(directory).resolve()
    manifest = json.loads((directory / 'manifest.json').read_text())
    if manifest.get('version') != 1 or set(manifest['files']) not in ({'wheel', 'requirements'}, {'wheel', 'requirements', 'python'}):
        raise ValueError('Invalid Skua bundle manifest')
    for kind, entry in manifest['files'].items():
        name = entry['name']
        if Path(name).name != name or name in {'.', '..'}:
            raise ValueError('Invalid bundle filename')
        path = directory / name
        if not re.fullmatch('[a-f0-9]{64}', entry['sha256']) or hashlib.sha256(path.read_bytes()).hexdigest() != entry['sha256']:
            raise ValueError('Skua bundle hash mismatch')
        if kind == 'wheel' and not re.fullmatch(r'skua-[A-Za-z0-9_.+-]+\.whl', name):
            raise ValueError('Expected pinned wheel')
        if kind == 'python' and not re.fullmatch(r'cpython-3\.12\.[0-9]+\+[0-9]+-x86_64-unknown-linux-gnu-install_only\.tar\.gz', name):
            raise ValueError('Expected standalone Linux amd64 Python archive')
    return directory, manifest


class SkuaOptions(InstalledAgentOptions):
    bundle_path: str | None = None
    max_seconds: float = Field(default=300, gt=0, allow_inf_nan=False)
    max_tool_calls: int = Field(default=100, gt=0)
    auth_enabled: bool = True
    git_bootstrap: GitBootstrap = 'default'
    qemu_runtime_check: bool = False


class SkuaAgent(BaseInstalledAgent):
    capabilities = AgentCapabilities(atif=True, base_url=True)
    options_model = SkuaOptions
    MODEL_CONNECTION = ModelConnectionSpec(
        default_provider='openai',
        supported_api_formats=[ModelApiFormat.OPENAI_CHAT_COMPLETIONS],
        default_api_format_by_provider={'default': ModelApiFormat.OPENAI_CHAT_COMPLETIONS},
        agent_provider_access=ProviderAccess(api_key_candidate_names=['SKUA_API_KEY'],
                                             base_url_candidate_names=['SKUA_BASE_URL']),
        api_key_destination_names={'default': 'SKUA_API_KEY'},
        base_url_destination_names={'default': 'SKUA_BASE_URL'},
    )

    @staticmethod
    def name():
        return 'skua'

    def __init__(self, logs_dir, *, bundle_path, max_seconds=300, max_tool_calls=100,
                 auth_enabled=True, git_bootstrap='default', qemu_runtime_check=False, **kwargs):
        if version('harbor') != HARBOR_VERSION:
            raise RuntimeError(f'This adapter requires harbor=={HARBOR_VERSION}')
        self.bundle, self.manifest = checked_bundle(bundle_path)
        selected = SkuaOptions(max_seconds=max_seconds, max_tool_calls=max_tool_calls, auth_enabled=auth_enabled, git_bootstrap=git_bootstrap, qemu_runtime_check=qemu_runtime_check)
        self.git_bootstrap = selected.git_bootstrap
        self.qemu_runtime_check = selected.qemu_runtime_check
        self.max_seconds = selected.max_seconds
        self.max_tool_calls = selected.max_tool_calls
        if not math.isfinite(self.max_seconds) or self.max_seconds <= 0 or self.max_tool_calls < 1:
            raise ValueError('Execution budgets must be positive and finite')
        if auth_enabled not in (True, False, 'true', 'false'):
            raise ValueError('auth_enabled must be true or false')
        self.auth_enabled = auth_enabled in (True, 'true')
        super().__init__(logs_dir=Path(logs_dir), bundle_path=str(self.bundle),
                         max_seconds=self.max_seconds, max_tool_calls=self.max_tool_calls,
                         auth_enabled=self.auth_enabled, git_bootstrap=self.git_bootstrap, qemu_runtime_check=self.qemu_runtime_check, **kwargs)
        if self.mcp_servers or self.skills_dir or self.load_trajectory:
            raise ValueError('MCP, skills, and trajectory loading are unsupported')
        if not self.model_name:
            raise ValueError('An explicit model is required')
        self.logs_dir.mkdir(parents=True, exist_ok=True)

    def version(self):
        return 'sha256:' + self.manifest['files']['wheel']['sha256']

    async def install(self, environment):
        # Recheck at upload time; a mutable local path is not an artifact pin.
        checked_bundle(self.bundle)
        # File discovery and patch repository checks need Git even with managed
        # commits disabled. Install it before any model request, or fail setup.
        await self.exec_as_root(environment, git_bootstrap_command(self.git_bootstrap), timeout_sec=300)
        if self.qemu_runtime_check:
            await self.exec_as_root(environment, QEMU_RUNTIME_CHECK, timeout_sec=15)
        await self.exec_as_root(environment, f'mkdir -p {INSTALL}; chmod 755 {INSTALL}')
        for entry in self.manifest['files'].values():
            await environment.upload_file(self.bundle / entry['name'], str(INSTALL / entry['name']))
        await environment.upload_file(Path(__file__).with_name('runner.py'), str(INSTALL / 'runner.py'))
        checks = '\n'.join(f"{entry['sha256']}  {entry['name']}" for entry in self.manifest['files'].values()) + '\n'
        wheel = shlex.quote(self.manifest['files']['wheel']['name'])
        requirements = shlex.quote(self.manifest['files']['requirements']['name'])
        python = 'python3'
        prepare = ''
        if 'python' in self.manifest['files']:
            archive = shlex.quote(self.manifest['files']['python']['name'])
            prepare = f'test "$(uname -m)" = x86_64 && tar -xzf {archive} && '
            python = './python/bin/python3'
        command = (f"printf %s {shlex.quote(checks)} | sha256sum -c - && "
            f"{prepare}{python} -c 'import sys; assert sys.version_info >= (3, 12)' && "
            f"{python} -m venv venv && "
            f"venv/bin/python -m pip install -r {requirements} && "
            f"venv/bin/python -m pip install --no-deps {wheel} && "
            "venv/bin/python -m pip check && chmod -R a+rX .")
        await self.exec_as_root(environment, command, cwd=str(INSTALL), timeout_sec=300)
        (self.logs_dir / 'artifact.json').write_text(json.dumps({
            'harbor_version': HARBOR_VERSION, 'adapter_version': ADAPTER_VERSION,
            'files': self.manifest['files'], 'git_bootstrap': self.git_bootstrap, 'qemu_runtime_check': self.qemu_runtime_check}, indent=2) + '\n')

    async def _upload_text(self, environment, text, target):
        with tempfile.TemporaryDirectory(prefix='skua-harbor-') as directory:
            source = Path(directory) / 'input'
            source.write_text(text)
            source.chmod(0o600)
            await self._upload_agent_owned_file(environment, source, str(target))
        await self.exec_as_agent(environment, f'chmod 600 {shlex.quote(str(target))}')

    async def run(self, instruction, environment, context):
        access = self.model_connection
        if access.model_api_format != ModelApiFormat.OPENAI_CHAT_COMPLETIONS:
            raise ValueError('Skua supports only Chat Completions')
        endpoint = self._base_url or access.user_supplied_base_url
        if not endpoint:
            raise ValueError('Supply an explicit --base-url or SKUA_BASE_URL')
        if self.auth_enabled and not access.api_key:
            raise ValueError('Configured credential environment variable is missing')
        cwd = await self.exec_as_agent(environment, 'pwd -P')
        root = cwd.stdout.strip()
        if not root.startswith('/') or '\n' in root:
            raise ValueError('Environment did not provide an absolute working directory')
        logs = self.environment_logs_dir
        await self.exec_as_agent(environment, f'mkdir -p {shlex.quote(str(logs))}')
        # Private inputs do not enter command arguments or generated model metadata.
        inputs = logs / 'inputs'
        await self.exec_as_agent(environment, f'mkdir -m 700 {shlex.quote(str(inputs))}')
        settings = {'root': root, 'base_url': endpoint, 'model': access.model_id or self.model_name,
                    'auth_enabled': self.auth_enabled, 'max_seconds': self.max_seconds,
                    'max_tool_calls': self.max_tool_calls}
        await self._upload_text(environment, self.render_instruction(instruction), inputs / 'task.txt')
        await self._upload_text(environment, json.dumps(settings), inputs / 'settings.json')
        command = f'exec {INSTALL}/venv/bin/python {INSTALL}/runner.py --settings {shlex.quote(str(inputs / "settings.json"))} --task {shlex.quote(str(inputs / "task.txt"))} --logs {shlex.quote(str(logs))}'
        code = None
        try:
            # Avoid BaseInstalledAgent's nonzero exception formatter, which includes
            # raw stdout/stderr. Skua's structured outcome is the authority here.
            result = await environment.exec(command=command, cwd=root,
                env={'SKUA_API_KEY': access.api_key} if self.auth_enabled else {},
                timeout_sec=math.ceil(self.max_seconds + 90))
            code = result.return_code
        finally:
            # These are exact adapter-owned input files, never model-generated paths.
            try:
                await environment.exec(command=f'rm -f {shlex.quote(str(inputs / "task.txt"))} {shlex.quote(str(inputs / "settings.json"))}', timeout_sec=10)
            except Exception:
                pass  # Still attempt artifact collection if container cleanup failed.
            for name in ('result.json', 'session.jsonl'):
                try:
                    await environment.download_file(str(logs / name), self.logs_dir / name)
                    (self.logs_dir / name).chmod(0o600)
                except Exception:
                    pass  # Outer timeout may precede even the first artifact write.
            self.populate_context_post_run(context)
        # A completed agent turn can leave an ordinary tool error (for example,
        # optional cleanup). Keep that native failure, but let Harbor judge the
        # task artifacts. Policy stops, interruptions and invalid runs still raise.
        verify_tool_failure = (code == 1 and
            context.metadata.get('skua_status') == 'failed' and
            context.metadata.get('skua_reason') == 'unresolved_tool_error')
        context.metadata['skua_exit_code'] = code
        if ((code != 0 or context.metadata.get('skua_status') != 'completed') and not verify_tool_failure
                or context.metadata.get('trajectory_export') == 'failed'):
            raise RuntimeError(f'Skua did not complete: {context.metadata.get("skua_status")} '
                               f'({context.metadata.get("skua_reason")}); exit code {code}; '
                               f'trajectory export: {context.metadata.get("trajectory_export", "not recorded")}; '
                               'inspect result.json and session.jsonl')

    def convert_trajectory(self, logs_dir):
        path = Path(logs_dir) / 'session.jsonl'
        if not path.exists() or not path.stat().st_size:
            return None
        return export_trajectory(path, wheel_sha256=self.manifest['files']['wheel']['sha256'])

    def populate_context_post_run(self, context):
        path = self.logs_dir / 'result.json'
        if path.exists() and path.stat().st_size:
            outcome = json.loads(path.read_text())
        else:
            outcome = {'status': 'failed', 'reason': 'outer_interruption_or_missing_artifacts',
                       'message': 'The outer run was interrupted or native result artifacts are missing.',
                       'next_step': 'Inspect the Harbor trial logs and any retained partial work.',
                       'task_success_verified': False, 'provider_usage': None}
            path.write_text(json.dumps(outcome) + '\n')
            path.chmod(0o600)
        # Fixed enum values only: never echo arbitrary native text into run metadata.
        statuses = {'completed', 'failed', 'cancelled', 'budget_exhausted', 'approval_required'}
        reasons = {'turn_completed', 'turn_error', 'unresolved_tool_error', 'execution_error',
                   'configuration_error', 'policy_review_required', 'recovery_pending',
                   'tool_limit', 'time_limit', 'signal', 'cancelled', 'input_cancelled',
                   'input_error', 'input_or_artifact_error', 'artifact_write_error',
                   'outer_interruption_or_missing_artifacts'}
        context.metadata = {'skua_status': outcome.get('status') if outcome.get('status') in statuses else 'failed',
                            'skua_reason': outcome.get('reason') if outcome.get('reason') in reasons else 'invalid_outcome',
                            'task_success_verified': False, 'provider_usage': None}
        try:
            trajectory = self.convert_trajectory(self.logs_dir)
        except (ValueError, KeyError, OSError):
            context.metadata['trajectory_export'] = 'failed'
            (self.logs_dir / 'trajectory-error.json').write_text('{"reason":"invalid_or_incomplete_native_history"}\n')
            return
        if trajectory is not None:
            target = self.logs_dir / 'trajectory.json'
            target.write_text(trajectory.model_dump_json(exclude_none=True, indent=2) + '\n')
            target.chmod(0o600)
