"""The same acceptance scenario runs against both source and installed wheels."""
import asyncio
import importlib.util
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location('installed_smoke', Path(__file__).parents[1] / 'scripts/installed_smoke.py')
smoke = importlib.util.module_from_spec(spec)
spec.loader.exec_module(smoke)


@pytest.mark.parametrize('shell', [p for p in ('/bin/bash', '/bin/zsh') if Path(p).exists()])
@pytest.mark.parametrize('streaming', [True, False])
def test_complete_workflow(workspace, monkeypatch, shell, streaming):
    monkeypatch.setattr('socket.socket.connect', smoke.blocked)
    monkeypatch.setattr('socket.create_connection', smoke.blocked)
    count = asyncio.run(smoke.workflow(workspace, shell=shell, streaming=streaming))
    assert count == 15


def test_readme_toml_examples_validate(workspace):
    import re
    from skua.config import build_config, parse_config
    readme = (Path(__file__).parents[1] / 'README.md').read_text()
    examples = re.findall(r'```toml\n(.*?)```', readme, flags=re.DOTALL)
    assert examples, 'README must include runnable configuration examples'
    base = {'schema_version': 1, 'profiles': {'default': {'base_url': 'https://mock.invalid/v1', 'model': 'test'}}}
    for index, source in enumerate(examples, 1):
        if not re.search(r'^schema_version\s*=', source, re.MULTILINE):
            source = 'schema_version = 1\n' + source
        parsed = parse_config(source.encode(), f'README example {index}')
        config = build_config(workspace, [('fixture', base), (f'example {index}', parsed)])
        assert config.profile().base_url and config.profile().model
