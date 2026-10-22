"""Optional one-time terminal startup animation and per-user first-run marker."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
from importlib.resources import files

from rich.console import Console, Group
from rich.cells import cell_len
from rich.live import Live
from rich.text import Text

MAX_ASSET_BYTES = 1_000_000
MAX_DIMENSION = 100


def _state_path() -> Path:
    if sys.platform == "darwin":
        base = Path.home() / "Library/Application Support"
    elif os.name == "nt":
        candidate = Path(os.environ.get("APPDATA", Path.home() / "AppData/Roaming"))
        base = candidate if candidate.is_absolute() else Path.home() / "AppData/Roaming"
    else:
        candidate = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local/state"))
        base = candidate if candidate.is_absolute() else Path.home() / ".local/state"
    return base / "skua" / "startup.json"


def _asset():
    raw = files("skua").joinpath("assets/startup-animation.json").read_bytes()
    if len(raw) > MAX_ASSET_BYTES:
        raise ValueError("asset too large")
    data = json.loads(raw)
    cols, rows = data["cols"], data["rows"]
    frames, delays = data["frames"], data["delays"]
    if (type(data["version"]) is not int or data["version"] != 1
            or type(cols) is not int or type(rows) is not int
            or not 1 <= cols <= MAX_DIMENSION or not 1 <= rows <= MAX_DIMENSION
            or not isinstance(frames, list) or not 1 <= len(frames) <= 100
            or not isinstance(delays, list) or len(delays) != len(frames)):
        raise ValueError("invalid animation header")
    validated = []
    for frame, delay in zip(frames, delays):
        if type(delay) is not int or not 20 <= delay <= 2000:
            raise ValueError("invalid frame delay")
        grid = frame["cells"]
        if not isinstance(grid, list) or len(grid) != rows:
            raise ValueError("invalid frame height")
        out = []
        for row in grid:
            if not isinstance(row, list) or len(row) != cols:
                raise ValueError("invalid frame width")
            rendered = Text()
            for cell in row:
                glyph, fg, bg = cell["g"], cell["fg"], cell["bg"]
                if (not isinstance(glyph, str) or len(glyph) != 1 or not glyph.isprintable()
                        or cell_len(glyph) != 1
                        or not isinstance(fg, list) or not isinstance(bg, list)
                        or len(fg) != 3 or len(bg) != 3
                        or any(type(c) is not int or not 0 <= c <= 255 for c in (*fg, *bg))):
                    raise ValueError("invalid animation cell")
                foreground = f"#{fg[0]:02x}{fg[1]:02x}{fg[2]:02x}"
                style = foreground if bg == [0, 0, 0] else f"{foreground} on #{bg[0]:02x}{bg[1]:02x}{bg[2]:02x}"
                rendered.append(glyph, style=style)
            out.append(rendered)
        validated.append((out, delay / 1000))
    return raw, validated


def _installation_key(raw: bytes) -> str:
    package_root = str(Path(__file__).resolve().parent)
    return hashlib.sha256((package_root + "\0" + hashlib.sha256(raw).hexdigest()).encode()).hexdigest()


def _seen(path: Path, key: str) -> bool:
    try:
        if path.stat().st_size > 64_000:
            return False
        data = json.loads(path.read_text(encoding="utf-8"))
        return isinstance(data, dict) and isinstance(data.get("seen"), list) and key in data["seen"]
    except (OSError, ValueError, TypeError):
        return False


def _mark(path: Path, key: str) -> None:
    name = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            data = json.loads(path.read_text(encoding="utf-8")) if path.stat().st_size <= 64_000 else {}
        except (OSError, ValueError, TypeError):
            data = {"version": 1, "seen": []}
        seen = data.get("seen") if isinstance(data, dict) else None
        if not isinstance(seen, list):
            seen = []
        if key not in seen:
            seen.append(key)
        fd, name = tempfile.mkstemp(prefix=".startup-", dir=path.parent)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump({"version": 1, "seen": seen[-100:]}, stream)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
        name = None
    except OSError:
        # The animation is cosmetic and must never prevent chat from starting.
        pass
    finally:
        if name:
            try:
                os.unlink(name)
            except OSError:
                pass


async def maybe_play(mode: str, console: Console | None = None) -> bool:
    """Play when configured; return whether an animation was actually shown."""
    if mode == "never" or not sys.stdout.isatty():
        return False
    console = console or Console()
    if not console.is_terminal or console.is_dumb_terminal:
        return False
    try:
        raw, frames = _asset()
    except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError):
        return False
    path = _state_path()
    key = _installation_key(raw)
    if mode == "once" and _seen(path, key):
        return False
    # Repaint only the animation's rows in the normal buffer, then erase them.
    # Crop to the viewport so frame updates never scroll old frames into history.
    try:
        import asyncio
        with Live(console=console, auto_refresh=False, transient=True,
                  vertical_overflow="crop") as live:
            try:
                for frame, delay in frames:
                    live.update(Group(*frame), refresh=True)
                    await asyncio.sleep(delay)
            finally:
                # Live.stop renders without cropping; erase before that redraw.
                live.update(Text())
    except (OSError, UnicodeError):
        return False
    _mark(path, key)
    return True
