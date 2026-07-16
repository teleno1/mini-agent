from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

import mini_agent.cli.app as cli_module
from mini_agent.adapters.session_store import SessionStore
from mini_agent.application.permissions import ConfirmationChoice, PermissionPolicyGate
from mini_agent.cli.app import create_app
from mini_agent.cli.presentation import ConversationPresenter, TerminalPermissionInteraction
from mini_agent.configuration import PermissionMode
from mini_agent.domain.messages import ToolResultMessage
from mini_agent.domain.sessions import SessionEventType
from mini_agent.domain.streams import (
    ResponseCompleted,
    ResponseStarted,
    TextDelta,
    ToolCallCompleted,
    ToolCallStarted,
)
from mini_agent.providers.fake import ScriptedFakeModelProvider
from mini_agent.tools.contracts import (
    NormalizedToolCall,
    PermissionDecision,
    PermissionRequest,
    RiskAssessment,
    SideEffectCategory,
    ToolCall,
)

runner = CliRunner()


def _tool_response(
    name: str,
    call_id: str,
    arguments: dict[str, object],
) -> tuple[object, ...]:
    return (
        ResponseStarted(request_id=f"request-{call_id}"),
        ToolCallStarted(tool_call_id=call_id, name=name),
        ToolCallCompleted(tool_call_id=call_id, arguments=arguments),
        ResponseCompleted(stop_reason="tool_calls"),
    )


def _text_response(text: str) -> tuple[object, ...]:
    return (
        ResponseStarted(request_id="request-final"),
        TextDelta(text=text),
        ResponseCompleted(),
    )


def _fake_app(
    tool_name: str,
    arguments: dict[str, object],
    provider_box: list[ScriptedFakeModelProvider] | None = None,
):
    def factory(configuration, tool_definitions, id_generator):
        del configuration, tool_definitions, id_generator
        provider = ScriptedFakeModelProvider(
            responses=(
                _tool_response(tool_name, "call-confirmation", arguments),
                _text_response("The requested operation was denied."),
            )
        )
        if provider_box is not None:
            provider_box.append(provider)
        return provider

    return create_app(factory)


def _permission_request() -> PermissionRequest:
    call = ToolCall(
        tool_call_id="call-confirmation",
        name="create_file",
        arguments={"path": "new.txt", "content": "created"},
    )
    return PermissionRequest(
        NormalizedToolCall.from_call(call),
        RiskAssessment(
            side_effect=SideEffectCategory.WRITE,
            resources=("new.txt",),
            summary="create one file",
        ),
    )


class _InteractiveChoiceScript:
    is_interactive = True

    def __init__(self, choices: list[ConfirmationChoice]) -> None:
        self._choices = iter(choices)

    def confirm(self, _preview):
        return next(self._choices)


def test_noninteractive_terminal_interaction_denies_before_prompt(monkeypatch) -> None:
    presenter = ConversationPresenter(output=lambda _text: None)
    interaction = TerminalPermissionInteraction(presenter, interactive=False)

    def unexpected_prompt(*_args, **_kwargs):
        raise AssertionError("non-interactive permission must not prompt")

    monkeypatch.setattr("mini_agent.cli.presentation.typer.prompt", unexpected_prompt)
    gate = PermissionPolicyGate(PermissionMode.SUGGEST, interaction=interaction)

    assert gate.decide(_permission_request()) is PermissionDecision.DENY
    assert gate.last_metadata.matched_rule == "non-interactive-input"
    assert "not interactive" in gate.last_metadata.reason


def test_confirmation_without_a_terminal_capability_fails_closed() -> None:
    gate = PermissionPolicyGate(
        PermissionMode.SUGGEST,
        interaction=lambda _preview: "allow-once",
    )

    assert gate.decide(_permission_request()) is PermissionDecision.DENY
    assert gate.last_metadata.matched_rule == "non-interactive-input"


def test_interactive_session_grant_requires_an_exact_argument_hash() -> None:
    request = _permission_request()
    changed_call = ToolCall(
        tool_call_id="call-changed",
        name="create_file",
        arguments={"path": "new.txt", "content": "changed"},
    )
    changed = PermissionRequest(NormalizedToolCall.from_call(changed_call), request.risk)
    gate = PermissionPolicyGate(
        PermissionMode.SUGGEST,
        interaction=_InteractiveChoiceScript(
            [ConfirmationChoice.ALLOW_FOR_SESSION, ConfirmationChoice.DENY]
        ),
    )

    assert gate.decide(request) is PermissionDecision.ALLOW
    assert gate.decide(changed) is PermissionDecision.DENY


@pytest.mark.parametrize(
    ("choice", "decision"),
    [
        ("allow-once", PermissionDecision.ALLOW),
        ("allow-exact-for-session", PermissionDecision.ALLOW),
        ("deny", PermissionDecision.DENY),
        ("cancel", PermissionDecision.CANCEL),
    ],
)
def test_interactive_confirmation_accepts_the_four_focused_choices(
    monkeypatch,
    choice: str,
    decision: PermissionDecision,
) -> None:
    presenter = ConversationPresenter(output=lambda _text: None, interactive=True)
    interaction = TerminalPermissionInteraction(presenter, interactive=True)
    monkeypatch.setattr(
        "mini_agent.cli.presentation.typer.prompt",
        lambda *_args, **_kwargs: choice,
    )

    gate = PermissionPolicyGate(PermissionMode.SUGGEST, interaction=interaction)

    assert gate.decide(_permission_request()) is decision


def test_interactive_confirmation_rejects_unlisted_affirmative_alias(monkeypatch) -> None:
    answers = iter(["allow", "allow-once"])
    presenter = ConversationPresenter(output=lambda _text: None, interactive=True)
    interaction = TerminalPermissionInteraction(presenter, interactive=True)
    monkeypatch.setattr(
        "mini_agent.cli.presentation.typer.prompt",
        lambda *_args, **_kwargs: next(answers),
    )

    gate = PermissionPolicyGate(PermissionMode.SUGGEST, interaction=interaction)

    assert gate.decide(_permission_request()) is PermissionDecision.ALLOW


@pytest.mark.parametrize("piped_value", ["", "allow\n", "session\n"])
def test_noninteractive_one_shot_eof_or_piped_affirmative_denies_without_prompt(
    tmp_path: Path,
    piped_value: str,
) -> None:
    providers: list[ScriptedFakeModelProvider] = []
    result = runner.invoke(
        _fake_app(
            "create_file",
            {"path": "new.txt", "content": "created"},
            providers,
        ),
        ["--workspace", str(tmp_path), "create a file"],
        input=piped_value,
    )

    assert result.exit_code == 0
    assert "Permission needed" not in result.stdout
    assert "Choose [" not in result.stdout
    assert "create_file (new.txt) - denied" in result.stdout
    assert not (tmp_path / "new.txt").exists()

    session = SessionStore(tmp_path).list_sessions()[0]
    events = SessionStore(tmp_path).read(session.session_id).events
    validated = next(
        event for event in events if event.event_type == SessionEventType.TOOL_VALIDATED
    )
    permission = validated.payload["permission"]
    assert permission["tool_call_id"] == "call-confirmation"
    assert permission["decision"] == "deny"
    assert permission["matched_rule"] == "non-interactive-input"
    assert permission["resource_summary"] == ["new.txt"]
    assert "content" not in permission
    failed = next(event for event in events if event.event_type == SessionEventType.TOOL_FAILED)
    assert failed.payload["tool_call_id"] == "call-confirmation"
    assert failed.payload["outcome"] == "denied"
    assert failed.causation_id == validated.event_id
    error = failed.payload["result"]["error"]
    assert error["code"] == "non-interactive-permission"
    assert "not interactive" in error["message"]
    final_request = providers[0].requests[1]
    tool_results = [
        item.message
        for item in final_request.messages
        if isinstance(item.message, ToolResultMessage)
    ]
    assert len(tool_results) == 1
    assert tool_results[0].outcome == "denied"


@pytest.mark.parametrize(
    ("tool_name", "arguments", "prepare_workspace"),
    [
        (
            "create_file",
            {"path": "new.txt", "content": "created"},
            lambda _workspace: None,
        ),
        (
            "apply_patch",
            {
                "operations": [
                    {
                        "operation": "update",
                        "path": "AGENTS.md",
                        "old_text": "old",
                        "new_text": "new",
                    }
                ]
            },
            lambda workspace: (workspace / "AGENTS.md").write_text("old\n", encoding="utf-8"),
        ),
        (
            "shell",
            {"command": "unknown-local-program", "working_directory": "."},
            lambda _workspace: None,
        ),
    ],
)
def test_noninteractive_confirmation_required_operations_are_denied_without_prompt(
    tmp_path: Path,
    tool_name: str,
    arguments: dict[str, object],
    prepare_workspace,
) -> None:
    prepare_workspace(tmp_path)
    result = runner.invoke(
        _fake_app(tool_name, arguments),
        ["--workspace", str(tmp_path), "perform the operation"],
        input="allow\n",
    )

    assert result.exit_code == 0
    assert "Choose [" not in result.stdout
    assert " - denied" in result.stdout
    store = SessionStore(tmp_path)
    events = store.read(store.list_sessions()[0].session_id).events
    validated = next(
        event for event in events if event.event_type == SessionEventType.TOOL_VALIDATED
    )
    assert validated.payload["permission"]["decision"] == "deny"
    assert validated.payload["permission"]["matched_rule"] == "non-interactive-input"
    assert SessionEventType.TOOL_STARTED not in [event.event_type for event in events]


def test_interactive_terminal_retains_allow_once_and_denies_focus_choices(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setattr(cli_module, "_is_terminal_input", lambda: True)
    allow = runner.invoke(
        _fake_app("create_file", {"path": "allowed.txt", "content": "created"}),
        ["--workspace", str(tmp_path)],
        input="create a file\nallow-once\n/exit\n",
    )

    assert allow.exit_code == 0
    assert "Permission needed" in allow.stdout
    assert "Choose [allow-once/allow-exact-for-session/deny/cancel]" in allow.stdout
    assert "create_file (allowed.txt) - completed" in allow.stdout
    assert (tmp_path / "allowed.txt").read_text(encoding="utf-8") == "created"

    denied = runner.invoke(
        _fake_app("create_file", {"path": "denied.txt", "content": "created"}),
        ["--workspace", str(tmp_path)],
        input="create another file\ndeny\n/exit\n",
    )

    assert denied.exit_code == 0
    assert "Permission needed" in denied.stdout
    assert "create_file (denied.txt) - denied" in denied.stdout
    assert not (tmp_path / "denied.txt").exists()
