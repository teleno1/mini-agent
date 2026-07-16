from typer.testing import CliRunner

from mini_agent.cli.app import app, create_app
from mini_agent.providers.fake import fake_provider_factory

runner = CliRunner()
fake_app = create_app(fake_provider_factory)


def test_help_is_available_without_credentials_or_repository() -> None:
    result = runner.invoke(app, ["--help"])

    assert result.exit_code == 0
    assert "Mini Agent" in result.stdout
    assert "Phase" not in result.stdout
    assert "Actions" not in result.stdout


def test_version_is_available_without_credentials_or_repository() -> None:
    result = runner.invoke(app, ["--version"])

    assert result.exit_code == 0
    assert result.stdout.strip() == "mini-agent 0.1.0"


def test_cli_renders_a_minimal_conversational_fake_turn() -> None:
    result = runner.invoke(fake_app, ["Explain Mini Agent"])

    assert result.exit_code == 0
    assert "You: Explain Mini Agent" in result.stdout
    assert "Agent: Mini Agent is a small, inspectable coding agent." in result.stdout
    assert "Phase" not in result.stdout
    assert "Actions" not in result.stdout


def test_cli_renders_user_markup_as_literal_text() -> None:
    result = runner.invoke(fake_app, ["Show [bold]literal[/bold] text"])

    assert result.exit_code == 0
    assert "You: Show [bold]literal[/bold] text" in result.stdout


def test_cli_lists_and_resumes_a_durable_session(monkeypatch, tmp_path) -> None:
    monkeypatch.chdir(tmp_path)

    first = runner.invoke(fake_app, ["remember this"])
    assert first.exit_code == 0

    listing = runner.invoke(fake_app, ["sessions"])
    assert listing.exit_code == 0
    session_id = listing.stdout.splitlines()[0].split("\t", maxsplit=1)[0]

    resumed = runner.invoke(fake_app, ["resume", session_id, "continue this"])

    assert resumed.exit_code == 0
    assert "Agent: Mini Agent is a small, inspectable coding agent." in resumed.stdout
