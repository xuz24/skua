"""Ephemeral activity notifications; never part of saved conversation history."""
from dataclasses import dataclass, field
from time import monotonic


@dataclass(frozen=True)
class Activity:
    phase: str  # tool, approval, model
    operation: str
    call_seq: int | None = None
    started: float = field(default_factory=monotonic)
    status: str | None = None  # None while active; otherwise the tool outcome
    summary: str = ''

    @property
    def elapsed(self):
        return max(0, monotonic() - self.started)
