from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from typer.testing import CliRunner

import mini_agent.cli.app as cli_module
from mini_agent.adapters.session_store import SessionStore
from mini_agent.application.cancellation import InterruptController
from mini_agent.cli.app import app
from mini_agent.domain.sessions import SessionEventType
from mini_agent.domain.streams import (
    Failure,
    ResponseCompleted,
    ResponseFailed,
    ResponseStarted,
    TextDelta,
    ToolCallCompleted,
    ToolCallStarted,
    UsageReported,
)

runner = CliRunner()


class ScriptedProvider:
    def __init__(self, responses: list[list[object]]) -> None:
        self.responses = responses
        self.request_count = 0

    def stream(self, messages: object):
        del messages

        async def emit():
            index = min(self.request_count, len(self.responses) - 1)
            self.request_count += 1
            for event in self.responses[index]:
                yield event

        return emit()


def _text_response(text: str = "Done") -> list[object]:
    return [
        ResponseStarted(request_id="response"),
        TextDelta(text=text),
        UsageReported(input_tokens=1, output_tokens=1),
        ResponseCompleted(),
    ]


def _tool_response(name: str, call_id: str, arguments: dict[str, object]) -> list[object]:
    return [
        ResponseStarted(request_id=f"response-{call_id}"),
        ToolCallStarted(tool_call_id=call_id, name=name),
        ToolCallCompleted(tool_call_id=call_id, arguments=arguments),
        UsageReported(input_tokens=1, output_tokens=1),
        ResponseCompleted(stop_reason="tool_calls"),
    ]


def _use_provider(monkeypatch, responses: list[list[object]]) -> None:
    monkeypatch.setattr(
        cli_module,
        "ScriptedFakeModelProvider",
        lambda: ScriptedProvider(responses),
    )


def _interrupted_session(tmp_path: Path) -> tuple[SessionStore, str]:
    (tmp_path / "note.txt").parent.mkdir(parents=True, exist_ok=True)
    (tmp_path / "note.txt").write_text("recover me\n", encoding="utf-8")
    store = SessionStore(tmp_path)
    writer = store.create("interrupted", created_at=datetime.now(UTC))
    turn = writer.append(SessionEventType.TURN_STARTED, {}, turn_id="turn-1")
    user = writer.append(
        SessionEventType.USER_MESSAGE,
        {"role": "user", "content": "recover this"},
        turn_id=turn.turn_id,
        causation_id=turn.event_id,
    )
    request = writer.append(
        SessionEventType.MODEL_REQUEST_STARTED,
        {"request_id": "request-1", "message_count": 1},
        turn_id=turn.turn_id,
        causation_id=user.event_id,
    )
    completed = writer.append(
        SessionEventType.MODEL_REQUEST_COMPLETED,
        {"request_id": "request-1", "input_tokens": 1, "output_tokens": 1},
        turn_id=turn.turn_id,
        causation_id=request.event_id,
    )
    assistant = writer.append(
        SessionEventType.ASSISTANT_MESSAGE,
        {
            "content": "",
            "tool_calls": [
                {
                    "tool_call_id": "call-interrupted",
                    "name": "read_file",
                    "arguments": {"path": "note.txt"},
                }
            ],
        },
        turn_id=turn.turn_id,
        causation_id=completed.event_id,
    )
    writer.append(
        SessionEventType.TOOL_PROPOSED,
        {
            "tool_call_id": "call-interrupted",
            "name": "read_file",
            "arguments": {"path": "note.txt"},
        },
        turn_id=turn.turn_id,
        causation_id=assistant.event_id,
    )
    writer.append(
        SessionEventType.TOOL_VALIDATED,
        {
            "tool_call_id": "call-interrupted",
            "name": "read_file",
            "arguments": {"path": "note.txt"},
            "risk": {
                "side_effect": "read",
                "resources": ["note.txt"],
                "hazards": [],
                "summary": "read one file",
            },
        },
        turn_id=turn.turn_id,
    )
    writer.append(
        SessionEventType.TOOL_STARTED,
        {
            "tool_call_id": "call-interrupted",
            "name": "read_file",
            "recovery": {"arguments": {"path": "note.txt"}},
        },
        turn_id=turn.turn_id,
    )
    writer.close()
    return store, "interrupted"


def test_production_cli_streams_text_and_reports_completion_without_dashboard(
    tmp_path: Path,
) -> None:
    result = runner.invoke(app, ["--workspace", str(tmp_path), "explain this"])

    assert result.exit_code == 0
    assert "You: explain this" in result.stdout
    assert "Agent: Mini Agent is a small, inspectable coding agent." in result.stdout
    assert "Completed" in result.stdout
    assert "Outcome:" in result.stdout
    assert "Verification:" in result.stdout
    assert "Changed files:" in result.stdout
    assert "Unresolved work:" in result.stdout
    assert "Next action:" in result.stdout
    assert "Phase" not in result.stdout
    assert "Actions" not in result.stdout
    assert "context_window_tokens" not in result.stdout


def test_cli_exposes_init_and_config_views_without_credentials(tmp_path: Path) -> None:
    initialized = runner.invoke(app, ["init", "--yes", "--workspace", str(tmp_path)])

    assert initialized.exit_code == 0
    assert (tmp_path / ".mini-agent" / "config.toml").exists()
    assert ".mini-agent/" in (tmp_path / ".gitignore").read_text(encoding="utf-8")

    shown = runner.invoke(app, ["config", "show", "--workspace", str(tmp_path)])

    assert shown.exit_code == 0
    assert '"api_key": null' in shown.stdout
    assert '"source": "built-in"' in shown.stdout


def test_cli_streams_tool_activity_and_only_prompts_for_a_write(
    monkeypatch, tmp_path: Path
) -> None:
    (tmp_path / "README.md").write_text("hello\n", encoding="utf-8")
    _use_provider(
        monkeypatch,
        [
            _tool_response("read_file", "read-1", {"path": "README.md"}),
            _text_response("The file is readable."),
        ],
    )

    read = runner.invoke(app, ["--workspace", str(tmp_path), "read README"])

    assert read.exit_code == 0
    assert "read_file (README.md) - completed" in read.stdout
    assert "Permission needed" not in read.stdout

    _use_provider(
        monkeypatch,
        [
            _tool_response(
                "create_file",
                "create-1",
                {"path": "new.txt", "content": "created"},
            ),
            _text_response("I could not create it."),
        ],
    )
    denied = runner.invoke(
        app,
        ["--workspace", str(tmp_path), "create a file"],
        input="deny\n",
    )

    assert denied.exit_code == 0
    assert "Permission needed" in denied.stdout
    assert "Operation:" in denied.stdout
    assert "Resources: new.txt" in denied.stdout
    assert "Reason:" in denied.stdout
    assert "allow-once/allow-exact-for-session/deny/cancel" in denied.stdout
    assert "create_file (new.txt) - denied" in denied.stdout
    assert not (tmp_path / "new.txt").exists()


def test_cli_renders_complex_plan_and_compaction_as_semantic_lines(
    monkeypatch, tmp_path: Path
) -> None:
    (tmp_path / "one.txt").write_text("one\n", encoding="utf-8")
    (tmp_path / "two.txt").write_text("two\n", encoding="utf-8")
    _use_provider(
        monkeypatch,
        [
            [
                ResponseStarted(request_id="response-plan"),
                ToolCallStarted(tool_call_id="read-1", name="read_file"),
                ToolCallCompleted(tool_call_id="read-1", arguments={"path": "one.txt"}),
                ToolCallStarted(tool_call_id="read-2", name="read_file"),
                ToolCallCompleted(tool_call_id="read-2", arguments={"path": "two.txt"}),
                UsageReported(input_tokens=1, output_tokens=1),
                ResponseCompleted(stop_reason="tool_calls"),
            ],
            _text_response("Inspected both files."),
        ],
    )

    planned = runner.invoke(app, ["--workspace", str(tmp_path), "inspect both files"])

    assert planned.exit_code == 0
    assert "Plan:" in planned.stdout
    assert "Inspect the relevant repository code" in planned.stdout
    assert "read_file (one.txt) - completed" in planned.stdout
    assert "read_file (two.txt) - completed" in planned.stdout

    compact = tmp_path / "compact"
    compact.mkdir()
    (compact / ".mini-agent").mkdir()
    (compact / ".mini-agent" / "config.toml").write_text(
        "context_window_tokens = 100\nresponse_reserve_tokens = 1\n",
        encoding="utf-8",
    )
    compacted = runner.invoke(app, ["--workspace", str(compact), "compact this"])

    assert compacted.exit_code == 1
    assert "Context pressure detected" in compacted.stdout
    assert "Context compacted" in compacted.stdout
    assert "Context compaction failed" in compacted.stdout
    assert "Turn failed" in compacted.stdout


def test_cli_reports_redacted_provider_error_and_keeps_session_listable(
    monkeypatch, tmp_path: Path
) -> None:
    failure = Failure(
        category="network",
        code="offline",
        source="provider",
        redacted_description="Provider unavailable",
        retryable=False,
        required_user_action="retry later",
    )
    _use_provider(monkeypatch, [[ResponseStarted(request_id="r"), ResponseFailed(failure)]])

    result = runner.invoke(app, ["--workspace", str(tmp_path), "network task"])

    assert result.exit_code == 1
    assert "[stream incomplete]" in result.stdout
    assert "Turn failed" in result.stdout
    assert "Phase" not in result.stdout
    listed = runner.invoke(app, ["sessions", "--workspace", str(tmp_path)])
    assert listed.exit_code == 0
    assert "network task" in listed.stdout


def test_cli_shows_provider_retry_progress(monkeypatch, tmp_path: Path) -> None:
    retryable = Failure(
        category="network",
        code="temporary",
        source="provider",
        redacted_description="Provider temporarily unavailable",
        retryable=True,
        required_user_action="retry the request",
    )
    _use_provider(
        monkeypatch,
        [
            [ResponseStarted(request_id="r1"), ResponseFailed(retryable)],
            _text_response("Recovered."),
        ],
    )

    result = runner.invoke(app, ["--workspace", str(tmp_path), "retry task"])

    assert result.exit_code == 0
    assert "Provider retry 2/3" in result.stdout
    assert "Agent: Recovered." in result.stdout


def test_cli_acknowledges_cancellation_without_reporting_completion(
    monkeypatch, tmp_path: Path
) -> None:
    class CancelImmediately(InterruptController):
        def install(self) -> None:
            super().install()
            self.request_interrupt()

    monkeypatch.setattr(cli_module, "InterruptController", CancelImmediately)

    result = runner.invoke(app, ["--workspace", str(tmp_path), "cancel this"])

    assert result.exit_code == 1
    assert "Cancelling" in result.stdout
    assert "was preserved and was not reported as successful" in result.stdout
    assert "Completed" not in result.stdout


def test_cli_resume_exposes_inspect_exit_and_abandon_choices(tmp_path: Path) -> None:
    _store, session_id = _interrupted_session(tmp_path)

    inspected = runner.invoke(
        app,
        ["resume", session_id, "--workspace", str(tmp_path)],
        input="inspect\nexit\n",
    )

    assert inspected.exit_code == 0
    assert "Interrupted work found" in inspected.stdout
    assert "Inspection recorded" in inspected.stdout
    assert "history was left without a guessed result" in inspected.stdout

    _store, session_id = _interrupted_session(tmp_path / "abandon")
    abandoned = runner.invoke(
        app,
        ["resume", session_id, "--workspace", str(tmp_path / "abandon")],
        input="abandon\n",
    )

    assert abandoned.exit_code == 0
    assert "was abandoned" in abandoned.stdout
