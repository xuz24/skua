"""API-independent conversation state; persistence is owned by storage.py."""
from dataclasses import asdict, dataclass
import hashlib
import json
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
        self.compaction: dict | None = None
        self.active_turn: int | None = None
        self.name: str | None = None
        self.destination: Path | None = None
        self.saved_bytes: bytes | None = None
        self.lock = None
        self.exports: dict[str, bytes] = {}
        from .permissions import SessionPolicy
        self.permissions = SessionPolicy()
        self._saved_state = self._state()

    def _state(self):
        return hashlib.sha256(json.dumps([self.name, self.events, self.compaction], sort_keys=True).encode()).digest()

    @property
    def dirty(self) -> bool:
        return self._saved_state != self._state()

    def mark_saved(self):
        self._saved_state = self._state()

    @property
    def current_model(self) -> ModelIdentity:
        for event in reversed(self.events):
            if event['type'] == 'model_changed':
                return ModelIdentity(**event['current'])
        return self.initial_model

    def change_model(self, identity: ModelIdentity):
        if self.active_turn is not None:
            raise ValueError('cannot change model during a turn')
        if identity != self.current_model:
            self._append('model_changed', previous=asdict(self.current_model),
                         current=asdict(identity), reason='resume', confirmed=True)

    def close(self):
        self.permissions.revoke("all")
        if self.lock is not None:
            self.lock.close()
            self.lock = None

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

    def messages(self, *, after_seq: int = 0, through_seq: int | None = None) -> list[dict]:
        result = []
        for event in self.events:
            if event['seq'] <= after_seq:
                continue
            if through_seq is not None and event['seq'] > through_seq:
                break
            if event['type'] == 'message':
                text = event['text']
                if event['completion'] == 'interrupted':
                    text += '\n[This response was interrupted and is incomplete.]'
                message = {'role': event['role'], 'content': text}
                if event['call_seqs']:
                    calls = [self.events[seq - 1] for seq in event['call_seqs']]
                    message['tool_calls'] = [
                        {'id': f"skua_call_{call['seq']}", 'type': 'function',
                         'function': {'name': call['name'], 'arguments': json.dumps(call['arguments'])}}
                        for call in calls
                    ]
                result.append(message)
            elif event['type'] == 'tool_result':
                result.append({'role': 'tool', 'tool_call_id': f"skua_call_{event['call_seq']}",
                               'content': event['model_text']})
        return result

    def active_messages(self) -> list[dict]:
        """Reduced model context, while canonical events remain complete."""
        if not self.compaction:
            return self.messages()
        return [{'role': 'system', 'content': 'Summary of earlier conversation (user-approved compaction):\n'
                 + self.compaction['summary']}, *self.messages(after_seq=self.compaction['through_seq'])]

    def compaction_input(self, keep_turns: int = 2):
        """Return messages and complete event boundary for a useful old prefix."""
        if self.active_turn is not None:
            raise ValueError('cannot compact during a turn')
        users = [e for e in self.events if e['type'] == 'message' and e['role'] == 'user']
        if len(users) <= keep_turns:
            raise ValueError('There is not enough older conversation to compact.')
        starts = [i for i, event in enumerate(self.events) if event['type'] == 'message' and event['role'] == 'user']
        through = self.events[starts[-keep_turns]]['seq'] - 1
        if self.compaction and through <= self.compaction['through_seq']:
            raise ValueError('There are no additional older turns to compact.')
        if self.compaction:
            selected = [{'role': 'system', 'content': 'Summary of earlier conversation:\n' + self.compaction['summary']}]
            selected.extend(self.messages(after_seq=self.compaction['through_seq'], through_seq=through))
        else:
            selected = self.messages(through_seq=through)
        return selected, through
