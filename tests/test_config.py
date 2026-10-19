import os

import pytest

from skua.config import ConfigError, command_environment, load_config, resolve_credential, resolve_root
from conftest import BASE, put


def test_field_merge_false_arrays_and_provenance(workspace, global_file):
    put(global_file, BASE + '''
[commands]
dotenv_allowlist = ["APP_ENV"]
[lint]
enabled = true
command = "check"
[sensitive]
filenames = ["global-secret"]
''')
    project = workspace / ".skua/config.toml"
    put(project, '''schema_version = 1
[profiles.default]
model = "project-model"
stream = false
[commands]
dotenv_allowlist = []
[lint]
enabled = false
[sensitive]
filenames = ["project-secret"]
''')
    config = load_config()
    assert config.profile().model == "project-model"
    assert config.profile().base_url == "https://models.example.org/v1"
    assert config.profile().stream is False
    assert config.commands.dotenv_allowlist == ()
    assert config.lint.enabled is False
    assert config.lint.command == "check"
    assert config.sensitive.filenames == ("project-secret",)
    assert config.sources["profiles.default.model"] == str(project)
    assert config.sources["profiles.default.base_url"] == str(global_file)
    assert config.sources["profiles.default.auth.env_var"] == "built-in defaults"


@pytest.mark.parametrize("inherited,selector,expected_path,expected_name", [
    ('name = "global-env"', 'path = ".conda"', ".conda", None),
    ('path = ".global-conda"', 'name = "project-env"', None, "project-env"),
])
def test_conda_selector_overrides(workspace, global_file, inherited, selector, expected_path, expected_name):
    put(global_file, BASE + '\n[environment]\nkind = "conda"\n' + inherited)
    put(workspace / ".skua/config.toml", "schema_version = 1\n[environment]\n" + selector)
    config = load_config()
    assert config.environment.path == (workspace / expected_path if expected_path else None)
    assert config.environment.name == expected_name


def test_global_relative_environment_and_root_override(workspace, global_file):
    put(global_file, BASE + '\n[environment]\nkind = "venv"\npath = ".venv"\n')
    child = workspace / "child"
    child.mkdir()
    (workspace / ".git").mkdir()
    config = load_config("child")
    assert config.root == child
    assert config.environment.path == child / ".venv"
    assert config.notes_directory == child / ".notes"
    assert not (child / ".venv").exists()  # Configuration does not activate/create it.


@pytest.mark.parametrize("text,field", [
    ('schema_version = 2', 'schema_version'),
    ('schema_version = true', 'schema_version'),
    ('[commands]\ntimeout_seconds = 2', 'schema_version'),
    ('schema_version = 1\n[commands]\ntimeout_seconds = true', 'commands.timeout_seconds'),
    ('schema_version = 1\n[commands]\ntimeout_seconds = 0', 'commands.timeout_seconds'),
    ('schema_version = 1\n[commands]\ntimeout_seconds = "300"', 'commands.timeout_seconds'),
    ('schema_version = 1\n[commands]\ntimeot_seconds = 300', 'commands.timeot_seconds'),
    ('schema_version = 1\n[environment]\nkind = "conda"\nname = "x"\npath = "y"', 'environment'),
    ('schema_version = 1\n[environment]\nkind = "venv"', 'environment.path'),
    ('schema_version = 1\n[lint]\nenabled = true', 'lint.command'),
    ('schema_version = 1\n[output]\nretain_max_bytes = 1', 'output.retain_max_bytes'),
    ('schema_version = 1\n[sensitive]\npatterns = ["../secrets"]', 'sensitive.patterns'),
    ('schema_version = 1\n[sensitive]\nfilenames = ["secret/file"]', 'sensitive.filenames'),
    ('schema_version = 1\n[profiles.default]\nmodel = "x"', 'profiles.default.base_url'),
])
def test_invalid_configuration_reports_source_without_values(workspace, global_file, text, field):
    put(global_file, text)
    with pytest.raises(ConfigError) as error:
        load_config()
    assert str(global_file) in str(error.value)
    assert field in str(error.value)


def test_invalid_lower_layer_not_hidden_by_override(workspace, global_file):
    put(global_file, BASE + '\n[commands]\ntimeout_seconds = "SECRET_INVALID"')
    put(workspace / ".skua/config.toml", 'schema_version = 1\n[commands]\ntimeout_seconds = 10')
    with pytest.raises(ConfigError) as error:
        load_config()
    assert "SECRET_INVALID" not in str(error.value)
    assert str(global_file) in str(error.value)


@pytest.mark.parametrize("url", ["https://u:SECRET@host/v1", "https://host/v1?key=SECRET", "https://host/v1#SECRET", "file:///SECRET", "http://host:99999"])
def test_endpoint_rejects_secrets_and_invalid_urls(workspace, global_file, url):
    put(global_file, f'schema_version = 1\n[profiles.default]\nbase_url = "{url}"\nmodel = "m"')
    with pytest.raises(ConfigError) as error:
        load_config()
    assert "SECRET" not in str(error.value)


def test_parse_error_does_not_echo_source(workspace, global_file):
    put(global_file, 'schema_version = 1\nSECRET_TOKEN = invalid')
    with pytest.raises(ConfigError) as error:
        load_config()
    assert "SECRET_TOKEN" not in str(error.value)


def test_profiles_and_credentials_are_lazy(workspace, global_file):
    config = load_config()
    assert config.profiles == {}
    with pytest.raises(ConfigError, match="not configured"):
        config.profile()
    put(global_file, BASE + '\n[profiles.local]\nbase_url = "http://localhost:8000/v1"\nmodel = "local"\n[profiles.local.auth]\nenabled = false')
    config = load_config(profile="local")
    assert config.default_profile == "default"
    assert resolve_credential(config, {}) is None


def test_dotenv_literal_precedence_no_ancestor_or_mutation(workspace, global_file):
    put(global_file, BASE)
    put(workspace.parent / ".env", "SKUA_API_KEY=ancestor-secret\n")
    config = load_config()
    with pytest.raises(ConfigError, match="missing or empty"):
        resolve_credential(config, {})
    put(workspace / ".env", 'SKUA_API_KEY=${OTHER_KEY}\nOTHER_KEY=secret\n')
    before = dict(os.environ)
    assert resolve_credential(config, {}) == "${OTHER_KEY}"
    assert resolve_credential(config, {"SKUA_API_KEY": "inherited"}) == "inherited"
    with pytest.raises(ConfigError, match="missing or empty"):
        resolve_credential(config, {"SKUA_API_KEY": ""})
    assert dict(os.environ) == before


def test_command_allowlist_separate_from_key_resolution(workspace, global_file):
    put(global_file, BASE + '\n[commands]\ndotenv_allowlist = ["APP_ENV", "NEW_VAR"]')
    put(workspace / ".env", "SKUA_API_KEY=secret\nAPP_ENV=dotenv\nNEW_VAR=added\nOTHER=excluded\n")
    config = load_config()
    inherited = {"APP_ENV": "inherited", "PATH": "/bin"}
    assert command_environment(config, inherited) == {**inherited, "NEW_VAR": "added"}
    assert inherited == {"APP_ENV": "inherited", "PATH": "/bin"}
    assert resolve_credential(config, {}) == "secret"


def test_root_must_be_directory(workspace):
    with pytest.raises(ConfigError, match="existing directory"):
        resolve_root("missing")
