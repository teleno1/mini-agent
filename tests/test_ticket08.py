from __future__ import annotations

import asyncio
import os
from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic import ValidationError

import mini_agent.tools.shell as shell_module
from mini_agent.adapters.clocks import DeterministicClock
from mini_agent.adapters.ids import DeterministicIdGenerator
from mini_agent.adapters.session_store import SessionStore
from mini_agent.application.agent import AgentTurnApplication
from mini_agent.application.permissions import ConfirmationChoice, PermissionPolicyGate
from mini_agent.configuration import EffectiveConfiguration, PermissionMode
from mini_agent.domain.sessions import SessionEventType
from mini_agent.domain.streams import (
    ResponseCompleted,
    ResponseStarted,
    TextDelta,
    ToolCallArgumentDelta,
    ToolCallCompleted,
    ToolCallStarted,
)
from mini_agent.providers.fake import ScriptedFakeModelProvider
from mini_agent.tools.contracts import (
    NormalizedToolCall,
    PermissionDecision,
    PermissionRequest,
    ToolCall,
    ToolOutcome,
    ToolRegistry,
)
from mini_agent.tools.shell import (
    MAX_OUTPUT_BYTES,
    MAX_TIMEOUT_SECONDS,
    ShellCommandClass,
    ShellInput,
    ShellTool,
    classify_shell_command,
    filtered_child_environment,
)
from mini_agent.tools.workspace import Workspace


class _InteractiveConfirmation:
    is_interactive = True

    def __init__(self, callback):
        self._callback = callback

    def confirm(self, preview):
        return self._callback(preview)


def _command_for_output(text: str) -> str:
    if os.name == "nt":
        return f"Write-Output '{text}'"
    return f"printf '%s' '{text}'"


def _command_for_sleep() -> str:
    return "Start-Sleep -Seconds 5" if os.name == "nt" else "sleep 5"


def _command_for_nested_marker(marker: str) -> str:
    if os.name == "nt":
        return (
            "powershell.exe -NoLogo -NoProfile -NonInteractive -Command "
            f'"Start-Sleep -Seconds 1; Set-Content -Path {marker} -Value gone"'
        )
    return f"sh -c 'sleep 1; printf gone > {marker}'"


def _command_for_exit(code: int) -> str:
    return f"exit {code}"


def test_shell_input_rejects_environment_overrides_and_caps_limits() -> None:
    with pytest.raises(ValidationError):
        ShellInput.model_validate({"command": "pwd", "env": {"SECRET": "value"}})
    with pytest.raises(ValidationError):
        ShellInput(command="pwd", timeout_seconds=MAX_TIMEOUT_SECONDS + 1)
    with pytest.raises(ValidationError):
        ShellInput(command="pwd", max_output_bytes=MAX_OUTPUT_BYTES + 1)


def test_shell_classifier_is_exact_and_explainable() -> None:
    cases = {
        "git status": (ShellCommandClass.LOCAL_READ, "recognized-local"),
        "pytest -q": (ShellCommandClass.LOCAL_TEST, "recognized-local"),
        "make build": (ShellCommandClass.LOCAL_BUILD, "recognized-local"),
        "cat 'literal > text'": (ShellCommandClass.LOCAL_READ, "recognized-local"),
        "cat a.txt && cat b.txt": (ShellCommandClass.CHAINING, "chaining"),
        "cat a.txt > output.txt": (ShellCommandClass.REDIRECTION, "redirection"),
        "python -m pytest": (ShellCommandClass.INTERPRETER, "interpreter"),
        "curl https://example.test": (ShellCommandClass.NETWORK, "network"),
        "npm install": (ShellCommandClass.INSTALL, "install"),
        "git commit -am message": (ShellCommandClass.GIT_WRITE, "git-write"),
        "rm -rf build": (ShellCommandClass.DELETION, "delete"),
        "unknown-local-program --check": (
            ShellCommandClass.UNKNOWN_EXECUTABLE,
            "unknown-executable",
        ),
        "vim file.txt": (ShellCommandClass.INTERACTIVE, "interactive"),
        "sleep 5 &": (ShellCommandClass.DETACHED, "detached"),
        "cat ../outside.txt": (ShellCommandClass.BOUNDARY_ESCAPE, "boundary-escape"),
        "find . -delete": (ShellCommandClass.DELETION, "delete"),
        'cat "$HOME/.ssh/id_rsa"': (
            ShellCommandClass.SENSITIVE_TARGET,
            "sensitive",
        ),
        'pytest "$HOME/test.py"': (
            ShellCommandClass.QUOTING_AMBIGUITY,
            "environment-expansion",
        ),
        "cat .env": (ShellCommandClass.SENSITIVE_TARGET, "sensitive"),
        "cat .env.example": (ShellCommandClass.LOCAL_READ, "recognized-local"),
        "C:/outside/git.exe status": (
            ShellCommandClass.BOUNDARY_ESCAPE,
            "boundary-escape",
        ),
        "./git status": (ShellCommandClass.UNKNOWN_EXECUTABLE, "unknown-executable"),
        'cat "unterminated': (ShellCommandClass.QUOTING_AMBIGUITY, "quoting-ambiguity"),
        "echo $(pwd)": (ShellCommandClass.QUOTING_AMBIGUITY, "quoting-ambiguity"),
        "echo `pwd`": (ShellCommandClass.QUOTING_AMBIGUITY, "quoting-ambiguity"),
    }
    for command, (category, hazard) in cases.items():
        classification = classify_shell_command(command)
        assert classification.category is category, command
        assert hazard in classification.hazards, command
        assert classification.reason
        assert classification.rule


def test_permission_modes_limit_full_auto_to_recognized_local_shell() -> None:
    tool = ShellTool()
    safe_input = ShellInput(command="git status")
    safe_risk = tool.assess(safe_input)
    safe_request = PermissionRequest(
        NormalizedToolCall.from_call(
            ToolCall(tool_call_id="safe", name="shell", arguments=safe_input.model_dump())
        ),
        safe_risk,
    )
    unsafe_input = ShellInput(command="unknown-local-program")
    unsafe_request = PermissionRequest(
        NormalizedToolCall.from_call(
            ToolCall(tool_call_id="unsafe", name="shell", arguments=unsafe_input.model_dump())
        ),
        tool.assess(unsafe_input),
    )

    assert (
        PermissionPolicyGate(PermissionMode.SUGGEST).decide(safe_request) is PermissionDecision.DENY
    )
    assert (
        PermissionPolicyGate(PermissionMode.AUTO_EDIT).decide(safe_request)
        is PermissionDecision.DENY
    )
    full_auto = PermissionPolicyGate(PermissionMode.FULL_AUTO)
    assert full_auto.decide(safe_request) is PermissionDecision.ALLOW
    assert full_auto.last_metadata.matched_rule == "full-auto-recognized-local"
    assert full_auto.decide(unsafe_request) is PermissionDecision.DENY
    for command in ('cat "$HOME/test.py"', "find . -delete", "cat .env"):
        risky = ShellInput(command=command)
        risky_request = PermissionRequest(
            NormalizedToolCall.from_call(
                ToolCall(tool_call_id=command, name="shell", arguments=risky.model_dump())
            ),
            tool.assess(risky),
        )
        assert full_auto.decide(risky_request) is PermissionDecision.DENY

    previews = []
    asking_gate = PermissionPolicyGate(
        PermissionMode.FULL_AUTO,
        interaction=_InteractiveConfirmation(
            lambda preview: previews.append(preview) or ConfirmationChoice.ALLOW_ONCE
        ),
    )
    assert asking_gate.decide(unsafe_request) is PermissionDecision.ALLOW
    assert len(previews) == 1
    assert previews[0].tool == "shell"
    assert previews[0].reason
    assert previews[0].argument_hash == unsafe_request.call.argument_hash


def test_child_environment_filters_provider_and_credential_values(monkeypatch) -> None:
    monkeypatch.setenv("MINI_AGENT_API_KEY", "mini-secret")
    monkeypatch.setenv("OPENAI_API_KEY", "provider-secret")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "cloud-secret")
    monkeypatch.setenv("AWS_SESSION_TOKEN", "session-secret")
    monkeypatch.setenv("NPM_TOKEN", "npm-secret")
    monkeypatch.setenv("DOCKER_AUTH_CONFIG", '{"auths":{}}')
    monkeypatch.setenv("PIP_INDEX_URL", "https://user:password@example.test/simple")
    monkeypatch.setenv("CUSTOM_SERVICE_URL", "https://user:password@example.test/api")
    monkeypatch.setenv("MINI_AGENT_TEST_VISIBLE", "visible")

    environment, removed = filtered_child_environment()

    assert "MINI_AGENT_API_KEY" not in environment
    assert "OPENAI_API_KEY" not in environment
    assert "AWS_SECRET_ACCESS_KEY" not in environment
    assert "AWS_SESSION_TOKEN" not in environment
    assert "NPM_TOKEN" not in environment
    assert "DOCKER_AUTH_CONFIG" not in environment
    assert "PIP_INDEX_URL" not in environment
    assert "CUSTOM_SERVICE_URL" not in environment
    assert environment["MINI_AGENT_TEST_VISIBLE"] == "visible"
    assert {
        "mini-secret",
        "provider-secret",
        "cloud-secret",
        "session-secret",
        "npm-secret",
        '{"auths":{}}',
        "https://user:password@example.test/simple",
        "https://user:password@example.test/api",
    }.issubset(set(removed))


@pytest.mark.asyncio
async def test_shell_rejects_interactive_and_out_of_bound_working_directories(
    tmp_path: Path,
) -> None:
    interactive = await ShellTool().execute(Workspace(tmp_path), ShellInput(command="vim file.txt"))
    assert interactive.outcome is ToolOutcome.DENIED
    assert interactive.error is not None and interactive.error.code == "unsupported-process"

    outside = await ShellTool().execute(
        Workspace(tmp_path), ShellInput(command="pwd", working_directory="../outside")
    )
    assert outside.outcome is ToolOutcome.DENIED
    assert outside.error is not None and outside.error.code == "traversal"

    absolute = await ShellTool().execute(
        Workspace(tmp_path),
        ShellInput(command="pwd", working_directory=str(tmp_path.parent)),
    )
    assert absolute.outcome is ToolOutcome.DENIED
    assert absolute.error is not None and absolute.error.code in {"absolute", "drive"}


@pytest.mark.asyncio
async def test_shell_runs_in_validated_directory_and_bounds_output(tmp_path: Path) -> None:
    nested = tmp_path / "nested"
    nested.mkdir()
    output = "x" * 400
    result = await ShellTool().execute(
        Workspace(tmp_path),
        ShellInput(
            command=_command_for_output(output),
            working_directory="nested",
            timeout_seconds=2,
            max_output_bytes=64,
        ),
    )

    assert result.outcome is ToolOutcome.SUCCESS
    assert result.data["working_directory"] == "nested"
    assert result.data["output_truncated"] is True
    assert result.data["output_bytes"] <= 64
    assert result.data["exit_code"] == 0
    assert result.data["duration_seconds"] >= 0


@pytest.mark.asyncio
async def test_shell_reports_exit_code_and_filters_child_environment(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("MINI_AGENT_API_KEY", "provider-secret")
    monkeypatch.setenv("MINI_AGENT_TEST_VISIBLE", "visible")
    if os.name == "nt":
        command = 'Write-Output "$env:MINI_AGENT_API_KEY|$env:MINI_AGENT_TEST_VISIBLE"'
    else:
        command = "printf '%s' \"$MINI_AGENT_API_KEY|$MINI_AGENT_TEST_VISIBLE\""

    result = await ShellTool().execute(
        Workspace(tmp_path),
        ShellInput(command=command),
    )
    assert result.outcome is ToolOutcome.SUCCESS
    assert "provider-secret" not in result.data["stdout"]
    assert "|visible" in result.data["stdout"]

    failed = await ShellTool().execute(
        Workspace(tmp_path),
        ShellInput(command=_command_for_exit(7)),
    )
    assert failed.outcome is ToolOutcome.FAILED
    assert failed.error is not None and failed.error.code == "exit-code"
    assert failed.data["exit_code"] == 7


@pytest.mark.asyncio
async def test_shell_timeout_terminates_process_group_and_closes_readers(tmp_path: Path) -> None:
    result = await ShellTool().execute(
        Workspace(tmp_path),
        ShellInput(command=_command_for_sleep(), timeout_seconds=0.05),
    )

    assert result.outcome is ToolOutcome.FAILED
    assert result.error is not None and result.error.code == "timeout"
    assert result.data["termination"] == "timeout"
    assert result.data["exit_code"] is not None


@pytest.mark.asyncio
async def test_shell_cancellation_cooperatively_interrupts_process_group(tmp_path: Path) -> None:
    task = asyncio.create_task(
        ShellTool().execute(
            Workspace(tmp_path),
            ShellInput(command=_command_for_sleep(), timeout_seconds=10),
        )
    )
    await asyncio.sleep(0.1)
    task.cancel()
    result = await task

    assert result.outcome is ToolOutcome.CANCELLED
    assert result.error is not None and result.error.code == "cancelled"
    assert result.data["termination"] == "cancelled"
    assert result.data["exit_code"] is not None


@pytest.mark.asyncio
async def test_shell_cancellation_reaches_a_nested_process_group(tmp_path: Path) -> None:
    marker = "nested-process-marker.txt"
    task = asyncio.create_task(
        ShellTool().execute(
            Workspace(tmp_path),
            ShellInput(command=_command_for_nested_marker(marker), timeout_seconds=10),
        )
    )
    await asyncio.sleep(0.1)
    task.cancel()
    result = await task

    assert result.outcome is ToolOutcome.CANCELLED
    assert result.data["exit_code"] is not None
    await asyncio.sleep(1.2)
    assert not (tmp_path / marker).exists()


@pytest.mark.asyncio
async def test_uncertain_shell_termination_is_interrupted(tmp_path: Path, monkeypatch) -> None:
    async def fake_spawn(*args, **kwargs):
        return object()

    async def fake_wait(*args, **kwargs):
        return shell_module._ShellExecution(
            returncode=None,
            stdout="",
            stderr="",
            output_truncated=False,
            duration_seconds=0.1,
            termination="cancelled",
            uncertain=True,
        )

    monkeypatch.setattr(shell_module, "_spawn_process", fake_spawn)
    monkeypatch.setattr(shell_module, "_wait_for_process", fake_wait)
    result = await ShellTool().execute(Workspace(tmp_path), ShellInput(command="sleep 5"))

    assert result.outcome is ToolOutcome.INTERRUPTED
    assert result.error is not None and result.error.code == "termination-uncertain"


@pytest.mark.asyncio
async def test_fake_agent_records_full_auto_shell_permission_and_lifecycle(
    tmp_path: Path,
) -> None:
    provider = ScriptedFakeModelProvider(
        responses=(
            (
                ResponseStarted(request_id="request-shell"),
                ToolCallStarted(tool_call_id="call-shell", name="shell"),
                ToolCallArgumentDelta(
                    tool_call_id="call-shell",
                    arguments='{"command":"pwd","working_directory":"."}',
                ),
                ToolCallCompleted(tool_call_id="call-shell"),
                ResponseCompleted(stop_reason="tool_calls"),
            ),
            (
                ResponseStarted(request_id="request-shell-final"),
                TextDelta(text="Checked the repository."),
                ResponseCompleted(),
            ),
        )
    )
    clock = DeterministicClock(datetime(2026, 1, 1, tzinfo=UTC))
    ids = DeterministicIdGenerator()
    store = SessionStore(tmp_path, clock=clock, id_generator=ids)
    configuration = EffectiveConfiguration(
        model="fake",
        permission_mode=PermissionMode.FULL_AUTO,
        provider_base_url="https://example.test/v1",
        max_model_requests=25,
        max_tool_calls=50,
        max_active_seconds=1800,
        context_window_tokens=1000,
        response_reserve_tokens=100,
        artifact_threshold_bytes=32 * 1024,
        instruction_file_bytes=32 * 1024,
        instruction_chain_bytes=128 * 1024,
    )
    application = AgentTurnApplication(
        provider=provider,
        workspace=Workspace(tmp_path),
        tool_registry=ToolRegistry([ShellTool()]),
        clock=clock,
        id_generator=ids,
        session_store=store,
        configuration=configuration,
    )

    result = await application.run("check the repository")
    events = store.read(result.session_id).events

    assert result.assistant_message.content == "Checked the repository."
    assert SessionEventType.TOOL_PROPOSED in [event.event_type for event in events]
    assert SessionEventType.TOOL_STARTED in [event.event_type for event in events]
    assert SessionEventType.TOOL_COMPLETED in [event.event_type for event in events]
    validated = next(
        event for event in events if event.event_type == SessionEventType.TOOL_VALIDATED
    )
    permission = validated.payload["permission"]
    assert permission["decision"] == "allow"
    assert permission["matched_rule"] == "full-auto-recognized-local"
    assert permission["scope"] == "none"
    assert permission["resource_summary"] == ["."]
    assert len(permission["argument_hash"]) == 64
    assert permission["timestamp"]
    assert permission["risk"]["hazards"] == ["recognized-local", "recognized-local-read"]
