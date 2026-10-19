from pathlib import Path

import pytest


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    home = tmp_path / "home"
    root = tmp_path / "project"
    home.mkdir()
    root.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.chdir(root)
    return root


@pytest.fixture
def global_file(workspace):
    return Path.home() / ".config" / "skua" / "config.toml"


def put(path, content):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)


BASE = '''schema_version = 1
[profiles.default]
base_url = "https://models.example.org/v1/"
model = "global-model"
'''
