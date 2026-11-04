"""Smooth startup retains theme colors, bounded timing, and fixed geometry."""
import asyncio
import io

from rich.console import Console
from rich.text import Text
import pytest

from skua.startup import _asset, _playback, _transition, maybe_play, FRAME_INTERVAL


def test_dissolve_is_monotonic_and_preserves_terminal_background():
    before = [Text(' ' * 16)]
    after = [Text('x' * 16, style='#abcdef')]
    counts = []
    for step in range(17):
        rows = _transition(before, after, step / 16)
        counts.append(rows[0].plain.count('x'))
        assert len(rows[0].plain) == 16
        # No invented opaque background or black-theme assumption.
        assert ' on ' not in str(rows[0].spans)
    assert counts == sorted(counts) and counts[0] == 0 and counts[-1] == 16
    assert 0 < counts[8] < 16
    assert _transition(before, after, 0) is before
    assert _transition(before, after, 1) is after


def test_playback_includes_original_poses_and_clears_within_one_second():
    _, source = _asset()
    playback = list(_playback(source))
    assert len(playback) > len(source)
    assert sum(delay for _, delay in playback) == pytest.approx(1)
    assert all(0 < delay <= FRAME_INTERVAL for _, delay in playback)
    poses = [[row.plain for row in frame] for frame, _ in playback]
    assert all([row.plain for row in frame] in poses for frame, _ in source)
    assert all(not row.strip() for row in poses[-1])
    assert all(len(pose) == 19 and all(len(row) == 48 for row in pose) for pose in poses)


def test_narrow_terminal_keeps_fixed_frame_geometry(workspace, monkeypatch):
    monkeypatch.setattr('skua.startup.sys.stdout', type('TTY', (), {'isatty': lambda _: True})())
    monkeypatch.setattr('skua.startup._state_path', lambda: workspace / 'startup.json')
    now = [0.0]
    monkeypatch.setattr('skua.startup.time.monotonic', lambda: now[0])
    async def advance(delay):
        now[0] += delay
    monkeypatch.setattr('asyncio.sleep', advance)
    frames = []
    class Live:
        def __init__(self, **kwargs):
            pass
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
        def update(self, frame, **kwargs):
            if hasattr(frame, 'renderables'):
                frames.append(frame.renderables)
                now[0] += .005  # Rendering time is part of the frame budget.
    monkeypatch.setattr('skua.startup.Live', Live)
    console = Console(file=io.StringIO(), force_terminal=True, width=20, height=8)
    assert asyncio.run(maybe_play('always', console))
    assert frames and all(len(frame) == 7 and all(len(row.plain) == 19 for row in frame) for frame in frames)
    assert now[0] == pytest.approx(1)
