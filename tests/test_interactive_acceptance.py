"""M5: identical edit/test/fix through the terminal and mocked HTTP boundary."""
import asyncio
import importlib
from pathlib import Path

import pytest


@pytest.mark.parametrize('shell', [p for p in ('/bin/bash', '/bin/zsh') if Path(p).exists()])
@pytest.mark.parametrize('streaming', [False, True])
def test_default_and_smoother_prompt_counts(workspace, monkeypatch, shell, streaming):
    monkeypatch.syspath_prepend(str(Path(__file__).parents[1] / 'scripts'))
    acceptance = importlib.import_module('interactive_acceptance')
    monkeypatch.setattr('socket.socket.connect', acceptance.blocked)
    monkeypatch.setattr('socket.create_connection', acceptance.blocked)
    reports = [asyncio.run(acceptance.workflow(workspace / mode, smoother=smoother,
               shell=shell, streaming=streaming))
               for mode, smoother in [('default', False), ('smoother', True)]]
    assert [r['operation_reviews'] for r in reports] == [5, 0]
    assert [r['grant_confirmations'] for r in reports] == [0, 2]
    assert [r['total_authorization_prompts'] for r in reports] == [5, 2]
    assert [r['commits'] for r in reports] == [1, 0]
    assert [r['transcript_saves'] for r in reports] == [0, 0]
