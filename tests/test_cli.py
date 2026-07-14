from typer.testing import CliRunner

from mini_agent.cli.app import app

runner = CliRunner()


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
    result = runner.invoke(app, ["Explain Mini Agent"])

    assert result.exit_code == 0
    assert "You: Explain Mini Agent" in result.stdout
    assert "Agent: Mini Agent is a small, inspectable coding agent." in result.stdout
    assert "Phase" not in result.stdout
    assert "Actions" not in result.stdout


def test_cli_renders_user_markup_as_literal_text() -> None:
    result = runner.invoke(app, ["Show [bold]literal[/bold] text"])

    assert result.exit_code == 0
    assert "You: Show [bold]literal[/bold] text" in result.stdout
