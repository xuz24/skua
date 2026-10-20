from dataclasses import asdict
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from skua.session import ModelIdentity, Session, timestamp
from skua.storage import Store, StorageError, Conflict, decode, encoded, snapshot


def state(root):
    session = Session(root, ModelIdentity('default', 'https://example/v1', 'model'))
    session.begin('hello')
    session.finish('answer')
    return session


def store(root):
    return Store(root, root / '.skua/sessions')


def save(session, storage, path=None):
    path = storage.destination(session, path)
    storage.save(session, path, snapshot(path))
    return path


def test_roundtrip_continue_identity_and_no_automatic_files(workspace):
    original = state(workspace)
    assert original.dirty and not list(workspace.iterdir())
    storage = store(workspace)
    path = save(original, storage)
    identity = original.session_id
    assert not original.dirty
    assert original.lock is not None and not os.get_inheritable(original.lock.fd)
    original.close()
    restored = storage.load(identity)
    try:
        assert restored.session_id == identity
        assert restored.events == original.events
        assert restored.destination == path
        assert not restored.dirty
        restored.begin('next')
        restored.finish('partial', 'cancelled', 'user_cancelled')
        assert restored.dirty
        save(restored, storage)
        assert len(decode(path.read_bytes(), workspace)[1]) == 6
    finally:
        restored.close()


def tool_session(root, status):
    session = Session(root, ModelIdentity('default', 'https://example/v1', 'model'))
    session.begin('run a command')
    session._append('message', role='assistant', text='', completion='complete', turn=1, call_seqs=[3])
    session._append('tool_call', turn=1, assistant_seq=2, name='run_command', arguments={'command':'echo hi'})
    session._append('approval', turn=1, call_seq=3, action='command', decision='denied' if status=='denied' else 'approved', reasons=['execute'], targets=[], summary='command', review_sha256='0'*64)
    session._append('tool_result', turn=1, call_seq=3, status=status, model_text='historical output', output=None, data={}, error=None)
    session.finish('done', 'cancelled' if status=='cancelled' else 'complete')
    return session


@pytest.mark.parametrize('status', ['ok','error','denied','cancelled','timeout','not_run'])
def test_tool_history_roundtrip_never_replays_approval(workspace, status):
    original = tool_session(workspace, status)
    storage = store(workspace)
    path = save(original, storage)
    original.close()
    restored = storage.load(str(path))
    try:
        assert restored.events == original.events
        context = restored.messages()
        assert context[1]['tool_calls'][0]['id'] == 'skua_call_3'
        assert context[2] == {'role':'tool','tool_call_id':'skua_call_3','content':'historical output'}
        assert not any('approved' in str(message) for message in context)
    finally:
        restored.close()


@pytest.mark.parametrize('mutate', [
    lambda rows: rows[0].update(schema_version=2),
    lambda rows: rows[0].update(schema_version=True),
    lambda rows: rows[0].update(extra='not allowed'),
    lambda rows: rows[0].update(event_count=99),
    lambda rows: rows[1].update(seq=2),
    lambda rows: rows[1].update(turn=True),
    lambda rows: rows[2].update(turn=99),
    lambda rows: rows[2].update(call_seqs=[99]),
    lambda rows: rows[3].update(type='unknown'),
    lambda rows: rows[0].update(initial_model={'profile':'default','model':'m','base_url':'https://user:secret@example/v1'}),
])
def test_corruption_rejected_with_line_and_file_unchanged(workspace, mutate):
    rows = [json.loads(line) for line in encoded(state(workspace), None).splitlines()]
    mutate(rows)
    data = ''.join(json.dumps(row)+'\n' for row in rows).encode()
    path = workspace/'broken.jsonl'
    path.write_bytes(data)
    with pytest.raises(StorageError, match='line') as error:
        store(workspace).load(str(path))
    assert 'secret' not in str(error.value)
    assert path.read_bytes() == data
    assert not (workspace/'.skua').exists()


@pytest.mark.parametrize('ending', [b'', b'\n\n', b'\n{"seq":NaN}\n'])
def test_truncated_blank_nonfinite_files(workspace, ending):
    data = encoded(state(workspace), None).rstrip(b'\n') + ending
    with pytest.raises(StorageError):
        decode(data, workspace)


def test_duplicate_keys_and_unfinished_turn(workspace):
    data = encoded(state(workspace), None).replace(b'"schema_version": 1', b'"schema_version": 1, "schema_version": 1')
    with pytest.raises(StorageError, match='line 1'):
        decode(data, workspace)
    session = state(workspace)
    session.begin('unfinished')
    with pytest.raises(StorageError, match='active work'):
        encoded(session, None)


def test_limits_and_unpaired_calls(workspace, monkeypatch):
    session = tool_session(workspace, 'ok')
    session.events[4]['call_seq'] = 999
    with pytest.raises(StorageError, match='call_seq'):
        encoded(session, None)
    data = encoded(state(workspace), None)
    monkeypatch.setattr('skua.storage.MAX_LINE_BYTES', 20)
    with pytest.raises(StorageError, match='oversized'):
        decode(data, workspace)


def test_atomic_failure_preserves_save_and_dirty_state(workspace, monkeypatch):
    session = state(workspace)
    storage = store(workspace)
    path = save(session, storage)
    before = path.read_bytes()
    session.begin('new')
    session.finish('change')
    def fail(*args):
        raise OSError('simulated disk failure')
    monkeypatch.setattr('skua.storage.os.replace', fail)
    with pytest.raises(StorageError, match='not replaced'):
        save(session, storage)
    assert path.read_bytes() == before and session.dirty
    assert session.lock is not None
    assert list(path.parent.iterdir()) == [path]
    session.close()


def test_stale_destination_and_explicit_overwrite(workspace):
    session = state(workspace)
    storage = store(workspace)
    path = workspace/'existing.jsonl'
    path.write_bytes(b'user data')
    with pytest.raises(Conflict, match='approval'):
        storage.save(session, path, b'user data')
    path.write_bytes(b'new user data')
    with pytest.raises(Conflict, match='changed'):
        storage.save(session, path, b'user data', overwrite=True)
    assert path.read_bytes() == b'new user data'
    storage.save(session, path, b'new user data', overwrite=True, name='Readable name')
    assert session.name == 'Readable name'
    assert not session.dirty
    session.close()


def test_export_unsaved_turns_escaped_closed_tools_no_jsonl(workspace):
    session = tool_session(workspace, 'denied')
    session.events[1]['text'] = '<script>unsafe</script>'
    session.events[2]['arguments'] = {'command':'</pre></details><script>bad</script>'}
    storage = store(workspace)
    path = workspace/'export.md'
    storage.export(session, path, None)
    text = path.read_text()
    assert '<script>' not in text and '&lt;script&gt;' in text
    assert '<details>' in text and '<details open' not in text
    assert session.dirty and session.destination is None
    assert not list(workspace.rglob('*.jsonl'))
    assert session.lock is None
    storage.export(session, path, path.read_bytes())
    assert session.dirty


def test_failed_export_preserves_jsonl_and_unsaved_state(workspace, monkeypatch):
    session = state(workspace)
    storage = store(workspace)
    jsonl = save(session, storage)
    before = jsonl.read_bytes()
    session.begin('unsaved')
    session.finish('new')
    def fail(*args):
        raise OSError('fail')
    monkeypatch.setattr('skua.storage.os.replace', fail)
    with pytest.raises(StorageError):
        storage.export(session, workspace/'out.md', None)
    assert jsonl.read_bytes() == before and session.dirty
    session.close()


def test_wrong_root_rejected_before_lock_creation(workspace):
    path = workspace/'saved.jsonl'
    path.write_bytes(encoded(state(workspace), None))
    other = workspace/'other'
    other.mkdir()
    with pytest.raises(StorageError, match='different project'):
        store(other).load(str(path))
    assert not (other/'.skua').exists()


def test_separate_process_lock_release_and_custom_path(workspace):
    session = state(workspace)
    path = workspace/'custom.jsonl'
    path.write_bytes(encoded(session, None))
    alias = workspace/'alias.jsonl'
    alias.write_bytes(path.read_bytes())
    script = '''
from pathlib import Path
import sys, time
from skua.storage import Store
s=Store(Path(sys.argv[1]),Path(sys.argv[1])/'.skua/sessions').load(sys.argv[2])
print('locked',flush=True)
time.sleep(60)
'''
    env = dict(os.environ, PYTHONPATH=str(Path(__file__).resolve().parents[1]))
    proc = subprocess.Popen([sys.executable,'-c',script,str(workspace),str(path)], stdout=subprocess.PIPE, text=True, env=env)
    try:
        assert proc.stdout.readline().strip() == 'locked'
        with pytest.raises(StorageError, match='locked'):
            store(workspace).load(str(alias))
    finally:
        proc.terminate()
        proc.wait(timeout=5)
        proc.stdout.close()
    restored = store(workspace).load(str(alias))
    restored.close()
    assert list((workspace/'.skua/locks').iterdir())  # Stable files are not removed.


def test_list_and_duplicate_names_and_id_lookup(workspace):
    storage = store(workspace)
    assert storage.list_sessions() == [] and not list(workspace.iterdir())
    identities = []
    for _ in range(2):
        session = state(workspace)
        storage.save(session, storage.destination(session), None, name='same name')
        identities.append(session.session_id)
        session.close()
    assert len(storage.list_sessions()) == 2
    restored = storage.load(identities[0])
    restored.close()
    duplicate = storage.directory/'copy.jsonl'
    duplicate.write_bytes((storage.directory/f'{identities[0]}.jsonl').read_bytes())
    with pytest.raises(StorageError, match='ambiguous'):
        storage.load(identities[0])


def test_lock_survives_atomic_save_and_cannot_overwrite_active_session(workspace):
    storage = store(workspace)
    first = state(workspace)
    path = save(first, storage)
    fd = first.lock.fd
    first.begin('next')
    first.finish('reply')
    save(first, storage)
    assert first.lock.fd == fd
    second = state(workspace)
    before = path.read_bytes()
    with pytest.raises(StorageError, match='locked'):
        storage.save(second, path, before, overwrite=True)
    assert path.read_bytes() == before
    first.close()
    storage.save(second, path, before, overwrite=True)
    second.close()


def test_model_change_chain_roundtrip_and_validation(workspace):
    session = state(workspace)
    session.change_model(ModelIdentity('other','https://other.example/v1','new'))
    header, events = decode(encoded(session, None), workspace)
    assert events[-1]['type'] == 'model_changed'
    session.events[-1]['previous']['model'] = 'wrong'
    with pytest.raises(StorageError, match='previous'):
        encoded(session, None)


def test_tool_output_validation(workspace):
    session = tool_session(workspace, 'ok')
    output = {'chunks':[{'stream':'stdout','text':'head'},{'omitted_bytes':10},{'stream':'stderr','text':'tail'}],
              'observed_bytes':18,'retained_bytes':8,'model_max_bytes':8,'retain_max_bytes':8,
              'capture_truncated':True,'model_truncated':True}
    session.events[4]['output'] = output
    decode(encoded(session, None), workspace)
    output['chunks'][1]['omitted_bytes'] = 9
    with pytest.raises(StorageError, match='omitted_bytes'):
        encoded(session, None)


def test_invalid_nested_values_are_safe_errors(workspace):
    session = tool_session(workspace, 'ok')
    session.events[2]['arguments']['bad'] = '\ud800'
    with pytest.raises(StorageError, match='invalid'):
        encoded(session, None)
    data = encoded(state(workspace), None).replace(b'"hello"',b'"\\ud800"')
    with pytest.raises(StorageError, match='line 2'):
        decode(data, workspace)


def test_foreign_project_session_cannot_be_overwritten(workspace):
    other = workspace/'other'
    other.mkdir()
    path = workspace/'other.jsonl'
    data = encoded(state(other), None)
    path.write_bytes(data)
    session = state(workspace)
    with pytest.raises(StorageError, match='another project'):
        store(workspace).save(session, path, data, overwrite=True)
    assert path.read_bytes() == data
    session.close()


def test_named_pipe_rejected_without_blocking(workspace):
    path = workspace/'pipe.jsonl'
    os.mkfifo(path)
    with pytest.raises(StorageError, match='regular file'):
        snapshot(path)
