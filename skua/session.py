"""API-independent conversation records held only in memory; no persistence."""
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4


def timestamp() -> str:
    return datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')


@dataclass(frozen=True)
class ModelIdentity:
    profile: str
    base_url: str
    model: str


class Session:
    def __init__(self, root: Path, identity: ModelIdentity):
        self.session_id = str(uuid4())
        self.project_root = root.resolve()
        self.created_at = timestamp()
        self.initial_model = identity
        self.events: list[dict] = []
        self.active_turn: int | None = None

    def _append(self, kind: str, **fields) -> int:
        seq = len(self.events) + 1
        self.events.append({'type': kind, 'seq': seq, 'at': timestamp(), **fields})
        return seq

    def begin(self, text: str) -> int:
        if self.active_turn is not None:
            raise ValueError('a turn is already active')
        self.active_turn = len(self.events) + 1
        return self._append('message', role='user', text=text, completion='complete',
                            turn=self.active_turn, call_seqs=[])

    def finish(self, text: str, status: str = 'complete', reason: str | None = None):
        if self.active_turn is None or status not in {'complete', 'cancelled', 'error'}:
            raise ValueError('invalid turn completion')
        if text or status == 'complete':
            self._append('message', role='assistant', text=text,
                         completion='complete' if status == 'complete' else 'interrupted',
                         turn=self.active_turn, call_seqs=[])
        self._append('turn_end', turn=self.active_turn, status=status, reason=reason)
        self.active_turn = None

    def notice(self, code: str, text: str):
        self._append('notice', turn=self.active_turn, code=code, text=text)

    def messages(self) -> list[dict]:
        result = []
        for event in self.events:
            if event['type'] == 'message':
                text = event['text']
                if event['completion'] == 'interrupted':
                    text += '\n[This response was interrupted and is incomplete.]'
                result.append({'role': event['role'], 'content': text})
        return result
