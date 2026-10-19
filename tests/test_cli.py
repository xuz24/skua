import json

from skua.cli import main
from conftest import BASE, put


def test_show_sources_never_reads_credentials(workspace, global_file, capsys, monkeypatch):
    put(global_file, BASE)
    put(workspace / ".env", "SKUA_API_KEY=SECRET_VALUE\n")
    monkeypatch.setattr("skua.config.read_dotenv", lambda _: (_ for _ in ()).throw(AssertionError("unexpected dotenv read")))
    assert main(["config", "show", "--sources"]) == 0
    output = capsys.readouterr().out
    assert "SECRET_VALUE" not in output
    report = json.loads(output)
    assert report["root"] == str(workspace)
    assert report["sources"]["profiles.default.model"] == str(global_file)


def test_credential_readiness_is_offline_and_redacted(workspace, global_file, capsys, monkeypatch):
    put(global_file, BASE)
    monkeypatch.setenv("SKUA_API_KEY", "SECRET_VALUE")
    assert main(["config", "check", "--credentials"]) == 0
    assert "SECRET_VALUE" not in capsys.readouterr().out


def test_flags_before_and_after_subcommands(workspace, global_file, capsys):
    put(global_file, BASE)
    child = workspace / "child"
    child.mkdir()
    for args in (["--root", "child", "config", "show"], ["config", "--root", "child", "show"], ["config", "show", "--root", "child"]):
        assert main(args) == 0
        assert json.loads(capsys.readouterr().out)["root"] == str(child)


def test_unconfigured_default_guides_to_setup(workspace, capsys):
    assert main([]) == 2
    assert "skua init" in capsys.readouterr().err
    assert main(["config", "check"]) == 0
    assert not (workspace / ".skua").exists()


def test_profile_override_does_not_change_defaults(workspace, global_file, capsys, monkeypatch):
    put(global_file, BASE + '\n[profiles.local]\nbase_url = "http://localhost/v1"\nmodel = "local"')
    assert main(["profiles", "--profile", "local"]) == 0
    assert "* local" in capsys.readouterr().out
    selected = []
    monkeypatch.setattr("skua.cli.run_chat", lambda config: selected.append(config.selected_profile) or 0)
    assert main([]) == 0
    assert selected == ["default"]


def test_local_diagnostics_no_profile_or_network(workspace, capsys, monkeypatch):
    monkeypatch.setattr('socket.socket.connect', lambda *args: (_ for _ in ()).throw(AssertionError('network')))
    monkeypatch.setattr('skua.config.resolve_credential', lambda *args: (_ for _ in ()).throw(AssertionError('credential lookup')))
    assert main(['squak', '--local']) == 0
    assert 'Local checks passed' in capsys.readouterr().out
    assert not (workspace / '.skua').exists()
    assert main(['squak']) == 2
    assert 'skua init' in capsys.readouterr().err
