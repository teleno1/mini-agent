from __future__ import annotations

import json
from pathlib import Path

from typer.testing import CliRunner

from mini_agent.adapters.session_store import SessionStore
from mini_agent.cli.app import app, create_app
from mini_agent.configuration import ConfigurationError
from mini_agent.diagnostics import DiagnosticLogger
from mini_agent.providers.fake import fake_provider_factory

runner = CliRunner()
fake_app = create_app(fake_provider_factory)


def _output(result) -> str:
    return getattr(result, "output", result.stdout)


def test_production_one_shot_missing_auth_fails_before_session_or_completion(
    monkeypatch, tmp_path: Path
) -> None:
    monkeypatch.delenv("MINI_AGENT_API_KEY", raising=False)

    result = runner.invoke(app, ["--workspace", str(tmp_path), "Explain Mini Agent"])

    output = _output(result)
    assert result.exit_code == 2
    assert "Provider authentication is unavailable" in output
    assert "MINI_AGENT_API_KEY" in output
    assert "never from TOML or CLI options" in output
    assert "Completed" not in output
    assert not SessionStore(tmp_path).list_sessions()


def test_production_interactive_missing_auth_fails_before_prompt(
    monkeypatch, tmp_path: Path
) -> None:
    monkeypatch.delenv("MINI_AGENT_API_KEY", raising=False)

    result = runner.invoke(app, ["--workspace", str(tmp_path)], input="/exit\n")

    assert result.exit_code == 2
    assert "Provider authentication is unavailable" in _output(result)
    assert "interactive Session" not in _output(result)


def test_production_resume_missing_auth_does_not_use_fake_provider(
    monkeypatch, tmp_path: Path
) -> None:
    monkeypatch.delenv("MINI_AGENT_API_KEY", raising=False)
    created = runner.invoke(fake_app, ["--workspace", str(tmp_path), "first task"])
    assert created.exit_code == 0
    session_id = SessionStore(tmp_path).list_sessions()[0].session_id

    result = runner.invoke(
        app,
        ["resume", session_id, "continue task", "--workspace", str(tmp_path)],
    )

    assert result.exit_code == 2
    assert "Provider authentication is unavailable" in _output(result)
    assert "Completed" not in _output(result)
    records = [
        json.loads(line)
        for line in (tmp_path / ".mini-agent" / "logs" / "diagnostic.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    assert records[-1]["failure"]["session_id"] == session_id


def test_blank_auth_is_actionable_and_secret_free(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("MINI_AGENT_API_KEY", "   ")

    result = runner.invoke(app, ["--workspace", str(tmp_path), "task"])

    assert result.exit_code == 2
    output = _output(result)
    assert "MINI_AGENT_API_KEY must not be blank" in output
    assert "Set a non-blank API key" in output
    assert "   " not in output
    assert "Completed" not in output


def test_offline_diagnostic_commands_work_without_provider_credentials(
    monkeypatch, tmp_path: Path
) -> None:
    monkeypatch.delenv("MINI_AGENT_API_KEY", raising=False)
    diagnostic = DiagnosticLogger(tmp_path).record_exception(ConfigurationError("offline check"))

    assert runner.invoke(app, ["--help"]).exit_code == 0
    assert runner.invoke(app, ["--version"]).exit_code == 0
    assert runner.invoke(app, ["init", "--yes", "--workspace", str(tmp_path)]).exit_code == 0
    assert runner.invoke(app, ["sessions", "--workspace", str(tmp_path)]).exit_code == 0
    shown = runner.invoke(app, ["config", "show", "--workspace", str(tmp_path)])
    assert shown.exit_code == 0
    assert '"api_key": null' in _output(shown)
    doctor = runner.invoke(
        app,
        ["doctor", diagnostic.error_id or "missing", "--workspace", str(tmp_path)],
    )
    assert doctor.exit_code == 0
    assert diagnostic.error_id in _output(doctor)


def test_explicit_fake_composition_supports_interactive_and_resume_without_network(
    monkeypatch, tmp_path: Path
) -> None:
    monkeypatch.delenv("MINI_AGENT_API_KEY", raising=False)

    interactive = runner.invoke(fake_app, ["--workspace", str(tmp_path)], input="/exit\n")
    assert interactive.exit_code == 0
    assert "interactive Session" in _output(interactive)
    assert "Session ended." in _output(interactive)

    created = runner.invoke(fake_app, ["--workspace", str(tmp_path), "first task"])
    assert created.exit_code == 0
    session_id = SessionStore(tmp_path).list_sessions()[0].session_id
    resumed = runner.invoke(
        fake_app,
        ["resume", session_id, "continue task", "--workspace", str(tmp_path)],
    )
    assert resumed.exit_code == 0
    assert "Agent: Mini Agent is a small, inspectable coding agent." in _output(resumed)
