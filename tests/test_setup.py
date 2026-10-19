import tomllib

import pytest

from skua.config import ConfigError, load_config
from skua.setup import run_setup, write_config
from conftest import BASE, put


def answers(overrides=None, save="y"):
    overrides = overrides or {}
    def ask(prompt):
        for label, answer in overrides.items():
            if prompt.startswith(label):
                return answer
        if prompt.startswith("Save this configuration?"):
            return save
        return ""
    return ask


def test_setup_new_local_profile_offline(workspace, global_file, monkeypatch):
    monkeypatch.setattr("socket.socket.connect", lambda *args: pytest.fail("setup attempted network access"))
    messages = []
    run_setup(workspace, ask=answers({
        "API base URL": "http://localhost:8000/v1", "Model identifier": "local-model",
        "Use API-key": "no",
    }), emit=messages.append)
    config = load_config()
    assert config.profile().model == "local-model"
    assert config.profile().auth.enabled is False
    assert global_file.exists()
    assert not (workspace / ".skua").exists()
    assert not (workspace / ".notes").exists()
    assert not (workspace / ".env").exists()


def test_new_named_profile_is_default(workspace):
    run_setup(workspace, profile="local", ask=answers({
        "API base URL": "http://localhost:8000/v1", "Model identifier": "local",
        "Use API-key": "no",
    }), emit=lambda _: None)
    assert load_config().selected_profile == "local"


def test_project_setup_does_not_copy_inherited_fields(workspace, global_file):
    put(global_file, BASE)
    run_setup(workspace, project=True, ask=answers({"Model identifier": "project-model"}), emit=lambda _: None)
    data = tomllib.loads((workspace / ".skua/config.toml").read_text())
    assert data == {"schema_version": 1, "profiles": {"default": {"model": "project-model"}}}
    assert global_file.read_text() == BASE


def test_comments_unrelated_values_and_blank_rerun(workspace, global_file):
    original = BASE + '\n# Keep my timeout.\n[commands]\ntimeout_seconds = 600 # seconds\n'
    put(global_file, original)
    assert run_setup(workspace, ask=answers(), emit=lambda _: None) is False
    assert global_file.read_text() == original
    run_setup(workspace, ask=answers({"Model identifier": "new-model"}), emit=lambda _: None)
    assert "# Keep my timeout." in global_file.read_text()
    assert "600 # seconds" in global_file.read_text()


def test_global_setup_never_copies_project_values(workspace, global_file):
    put(global_file, BASE)
    put(workspace / ".skua/config.toml", 'schema_version = 1\n[profiles.default]\nmodel = "project-only"')
    run_setup(workspace, ask=answers({"Stream responses": "no"}), emit=lambda _: None)
    assert "project-only" not in global_file.read_text()


def test_inherit_removes_override_disable_retains_command(workspace, global_file):
    put(global_file, BASE + '\n[lint]\nenabled = true\ncommand = "check"')
    path = workspace / ".skua/config.toml"
    put(path, 'schema_version = 1\n[profiles.default]\nmodel = "project"')
    run_setup(workspace, project=True, ask=answers({"Model identifier": ":inherit", "Enable a project lint": ":disable"}), emit=lambda _: None)
    config = load_config()
    assert config.profile().model == "global-model"
    assert config.lint.enabled is False
    assert config.lint.command == "check"


@pytest.mark.parametrize("response", ["", "n"])
def test_declined_setup_writes_nothing(workspace, global_file, response):
    run_setup(workspace, ask=answers({"API base URL": "http://localhost/v1", "Model identifier": "model"}, save=response), emit=lambda _: None)
    assert not global_file.exists()
    assert not global_file.parent.exists()


def test_cancelled_setup_writes_nothing(workspace, global_file):
    def interrupt(_):
        raise KeyboardInterrupt
    with pytest.raises(KeyboardInterrupt):
        run_setup(workspace, ask=interrupt)
    assert not global_file.exists()


def test_write_detects_stale_file(workspace, global_file):
    put(global_file, BASE)
    newer = BASE + "\n# Edited outside setup\n"
    global_file.write_text(newer)
    with pytest.raises(ConfigError, match="changed during setup"):
        write_config(global_file, BASE + "\n", BASE.encode())
    assert global_file.read_text() == newer


def test_failed_replace_preserves_file_and_cleans_temp(workspace, global_file, monkeypatch):
    put(global_file, BASE)
    def fail(*args):
        raise OSError("disk error")
    monkeypatch.setattr("skua.setup.os.replace", fail)
    with pytest.raises(ConfigError, match="could not save"):
        write_config(global_file, BASE + "\n", BASE.encode())
    assert global_file.read_text() == BASE
    assert list(global_file.parent.iterdir()) == [global_file]


def test_setup_venv_default_path(workspace, global_file):
    put(global_file, BASE)
    run_setup(workspace, ask=answers({"Command environment:": "venv"}), emit=lambda _: None)
    assert load_config().environment.path == workspace / ".venv"
