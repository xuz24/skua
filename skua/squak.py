"""Local probes and synthetic endpoint diagnostics; never dispatch model tools."""
import asyncio
from typing import Callable

from .config import Config, resolve_credential
from .client import ChatClient, check_capabilities
from .environment import prepare_command
from .execution import run_command


async def local_checks(config: Config, emit: Callable[[str], None] = print) -> int:
    invocation = prepare_command(config, ':')
    emit(f'Configuration: valid\nProject root: {config.root}')
    emit(f'Shell: {invocation.shell} (startup {"on" if config.shell.startup_enabled else "off"})')
    emit(f'Environment: {config.environment.kind}')
    if config.environment.kind != 'inherited':
        emit(f'Selected environment: {config.environment.path or config.environment.name}')
    emit('Running a fixed local activation probe; configured startup/activation scripts may run.')
    result = await run_command(config, ':')
    if result.stdout.total_bytes or result.stderr.total_bytes:
        emit(result.preview())
    if result.status != 'completed' or result.returncode:
        emit(f'Local probe failed: {result.status}, exit {result.returncode}')
        return 130 if result.status == 'cancelled' else 2
    emit('Local checks passed. No endpoint request or credential lookup performed.')
    return 0


def check_local(config: Config, emit: Callable[[str], None] = print) -> int:
    return asyncio.run(local_checks(config, emit))


async def full_checks(config: Config, emit=print, *, client_factory=ChatClient) -> int:
    status = await local_checks(config, emit)
    if status:
        return status
    emit('Endpoint diagnostic may consume tokens. Uses a synthetic request; no returned tool call is executed.')
    async with client_factory(config.profile(), resolve_credential(config)) as client:
        await check_capabilities(client, on_retry=lambda n, d: emit(f'Diagnostic retry {n}/3 in {d:g}s.'))
    emit('Endpoint and native tool-calling checks passed (' + ('streaming' if config.profile().stream else 'nonstreaming') + ').')
    return 0


def check_full(config: Config, emit=print) -> int:
    return asyncio.run(full_checks(config, emit))
