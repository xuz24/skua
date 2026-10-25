"""Internal subprocess runner. Callers must obtain authorization before dispatch."""
from __future__ import annotations

import asyncio
import errno
from dataclasses import dataclass
import os
import signal
import sys
import time
from typing import Mapping

from .config import Config, ConfigError
from .environment import prepare_command


@dataclass(frozen=True)
class Captured:
    head: bytes
    tail: bytes
    total_bytes: int

    @property
    def omitted_bytes(self) -> int:
        return self.total_bytes - len(self.head) - len(self.tail)

    def preview(self, limit: int) -> Captured:
        data = self.head + self.tail
        if len(data) <= limit:
            return self
        first = (limit + 1) // 2
        last = limit // 2
        return Captured(data[:first], data[-last:] if last else b'', self.total_bytes)

    def text(self) -> str:
        marker = f'\n... [{self.omitted_bytes} bytes omitted] ...\n' if self.omitted_bytes else ''
        if not marker:
            return (self.head + self.tail).decode('utf-8', errors='replace')
        return self.head.decode('utf-8', errors='replace') + marker + self.tail.decode('utf-8', errors='replace')


class Capture:
    def __init__(self, limit: int):
        self.first = (limit + 1) // 2
        self.last = limit // 2
        self.head = bytearray()
        self.tail = bytearray()
        self.total = 0

    def feed(self, data: bytes) -> None:
        self.total += len(data)
        take = min(self.first - len(self.head), len(data))
        self.head.extend(data[:take])
        if self.last:
            self.tail.extend(data[take:])
            if len(self.tail) > self.last:
                del self.tail[:-self.last]

    def snapshot(self) -> Captured:
        return Captured(bytes(self.head), bytes(self.tail), self.total)


@dataclass(frozen=True)
class CommandResult:
    returncode: int
    status: str  # completed, timeout, cancelled
    stdout: Captured
    stderr: Captured
    duration_seconds: float
    model_max_bytes: int

    def preview(self) -> str:
        out = self.stdout.preview((self.model_max_bytes + 1) // 2)
        err = self.stderr.preview(self.model_max_bytes // 2)
        discarded = self.stdout.omitted_bytes + self.stderr.omitted_bytes
        return (f'stdout:\n{out.text()}\nstderr:\n{err.text()}\n'
                f'[{discarded} bytes discarded beyond retention; previews may omit additional retained bytes]')


def _signal_group(pid: int, sig: int) -> bool:
    try:
        os.killpg(pid, sig)
        return True
    except ProcessLookupError:
        return False


async def _signal_group_after_reaping(pid: int, sig: int) -> bool:
    # Darwin can return EPERM for an exited, unreaped process group. Yield to
    # asyncio's child watcher and retry the *group*, not just the leader: live
    # descendants still need signalling. Never suppress persistent permission errors.
    deadline = time.monotonic() + 0.25
    while True:
        try:
            return _signal_group(pid, sig)
        except PermissionError as exc:
            if sys.platform != 'darwin' or exc.errno != errno.EPERM or time.monotonic() >= deadline:
                raise
            await asyncio.sleep(0.01)


async def _stop(proc: asyncio.subprocess.Process, grace: float) -> None:
    if not await _signal_group_after_reaping(proc.pid, signal.SIGTERM):
        return
    deadline = time.monotonic() + grace
    while time.monotonic() < deadline:
        if not await _signal_group_after_reaping(proc.pid, 0):
            return
        await asyncio.sleep(min(0.05, max(0, deadline - time.monotonic())))
    await _signal_group_after_reaping(proc.pid, signal.SIGKILL)


async def _finish_despite_cancellation(task):
    while True:
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            continue


async def run_command(config: Config, command: str, *, environ: Mapping[str, str] | None = None,
                      _grace_seconds: float = 5.0, _invocation=None) -> CommandResult:
    """Run an already-authorized command; cancellation returns captured output after cleanup.

    Retention and preview budgets are split equally across stdout/stderr (odd byte
    goes to stdout). No spill files, transcript writes, rollback, or Git operations.
    """
    invocation = _invocation or prepare_command(config, command, environ)
    started = time.monotonic()
    spawn = asyncio.create_task(asyncio.create_subprocess_exec(
        *invocation.argv, cwd=invocation.root, env=invocation.env,
        stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE, start_new_session=True, limit=65536,
    ))
    status = 'completed'
    try:
        try:
            proc = await asyncio.shield(spawn)
        except asyncio.CancelledError:
            proc = await _finish_despite_cancellation(spawn)
            status = 'cancelled'
    except OSError:
        raise ConfigError('command: could not start selected shell') from None
    out = Capture((config.output.retain_max_bytes + 1) // 2)
    err = Capture(config.output.retain_max_bytes // 2)

    async def drain(stream, capture):
        while chunk := await stream.read(65536):
            capture.feed(chunk)

    readers = [asyncio.create_task(drain(proc.stdout, out)), asyncio.create_task(drain(proc.stderr, err))]
    async def complete():
        await proc.wait()
        await asyncio.gather(*readers)
    completion = asyncio.create_task(complete())
    try:
        if status == 'completed':
            await asyncio.wait_for(asyncio.shield(completion), config.commands.timeout_seconds)
    except TimeoutError:
        status = 'timeout'
    except asyncio.CancelledError:
        status = 'cancelled'
    except Exception:
        status = 'failed'
        raise
    finally:
        if status != 'completed':
            async def cleanup():
                await _stop(proc, _grace_seconds)
                await completion
            await _finish_despite_cancellation(asyncio.create_task(cleanup()))
    return CommandResult(proc.returncode, status, out.snapshot(), err.snapshot(),
                         time.monotonic() - started, config.output.model_max_bytes)
