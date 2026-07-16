from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic import BaseModel

from mini_agent.adapters.clocks import DeterministicClock
from mini_agent.adapters.ids import DeterministicIdGenerator
from mini_agent.adapters.session_store import SessionStore
from mini_agent.application.agent import AgentTurnApplication
from mini_agent.context import ContextBuilder, ContextFrame, ContextLayerName
from mini_agent.domain.messages import ToolResultMessage
from mini_agent.domain.sessions import SessionEventType
from mini_agent.domain.streams import (
    ResponseCompleted,
    ResponseStarted,
    TextDelta,
    ToolCallCompleted,
    ToolCallStarted,
    UsageReported,
)
from mini_agent.providers.fake import ScriptedFakeModelProvider
from mini_agent.tools.contracts import (
    PermissionDecision,
    PermissionRequest,
    ToolLimits,
    ToolOutcome,
    ToolRegistry,
    ToolResult,
)
from mini_agent.tools.files import ReadFileTool
from mini_agent.tools.workspace import Workspace


class _CountingRegistry(ToolRegistry):
    def __init__(self, tools=()) -> None:
        super().__init__(tools)
        self.require_calls: list[str] = []

    def require(self, name: str):
        self.require_calls.append(name)
        return super().require(name)


class _RecordingPermissionGate:
    def __init__(self) -> None:
        self.requests: list[PermissionRequest] = []

    def decide(self, request: PermissionRequest) -> PermissionDecision:
        self.requests.append(request)
        return PermissionDecision.ALLOW


def _tool_response(
    call_id: str,
    name: str,
    arguments: dict[str, object],
    *,
    input_tokens: int,
    output_tokens: int,
):
    return (
        ResponseStarted(request_id=f"request-{call_id}"),
        ToolCallStarted(tool_call_id=call_id, name=name),
        ToolCallCompleted(tool_call_id=call_id, arguments=arguments),
        UsageReported(input_tokens=input_tokens, output_tokens=output_tokens),
        ResponseCompleted(stop_reason="tool_calls"),
    )


def _text_response(text: str, *, input_tokens: int, output_tokens: int):
    return (
        ResponseStarted(request_id="request-final"),
        TextDelta(text=text),
        UsageReported(input_tokens=input_tokens, output_tokens=output_tokens),
        ResponseCompleted(),
    )


class _SmallBoundReadFileTool(ReadFileTool):
    name = "small_read"
    limits = ToolLimits.bounded(timeout_seconds=30.0, max_output_bytes=1)


class _CountingReadFileTool(ReadFileTool):
    def __init__(self) -> None:
        self.execution_count = 0

    async def execute(self, workspace: Workspace, arguments: BaseModel) -> ToolResult:
        self.execution_count += 1
        return await super().execute(workspace, arguments)


@pytest.mark.asyncio
async def test_unknown_tool_is_invalid_once_and_corrected_call_recovers(
    tmp_path: Path,
    monkeypatch,
) -> None:
    (tmp_path / "note.txt").write_text("durable evidence\n", encoding="utf-8")

    async def unexpected_shell(*_args, **_kwargs):
        raise AssertionError("unknown Tool must not start a Shell process")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", unexpected_shell)
    provider = ScriptedFakeModelProvider(
        responses=(
            _tool_response(
                "call-unknown",
                "missing_tool",
                {"path": "new.txt"},
                input_tokens=3,
                output_tokens=4,
            ),
            _tool_response(
                "call-corrected",
                "read_file",
                {"path": "note.txt"},
                input_tokens=5,
                output_tokens=6,
            ),
            _text_response(
                "Recovered after the invalid observation.",
                input_tokens=7,
                output_tokens=8,
            ),
        )
    )
    read_tool = _CountingReadFileTool()
    registry = _CountingRegistry([read_tool])
    permission_gate = _RecordingPermissionGate()
    clock = DeterministicClock(datetime(2026, 1, 1, tzinfo=UTC))
    ids = DeterministicIdGenerator()
    store = SessionStore(tmp_path, clock=clock, id_generator=ids)
    application = AgentTurnApplication(
        provider=provider,
        workspace=Workspace(tmp_path),
        tool_registry=registry,
        clock=clock,
        id_generator=ids,
        session_store=store,
        context_builder=ContextBuilder(tmp_path),
        permission_gate=permission_gate,
    )

    result = await application.run("recover from an unknown tool")
    snapshot = store.read(result.session_id)

    assert result.assistant_message.content == "Recovered after the invalid observation."
    assert [item.outcome for item in result.tool_results] == [
        ToolOutcome.INVALID.value,
        ToolOutcome.SUCCESS.value,
    ]
    assert result.tool_results[0].tool_call_id == "call-unknown"
    assert result.tool_results[0].content == "Tool name is not registered"
    assert result.model_request_count == 3
    assert result.tool_call_count == 2
    assert (result.usage_input_tokens, result.usage_output_tokens) == (15, 18)
    assert registry.require_calls.count("missing_tool") == 1
    assert read_tool.execution_count == 1
    assert [request.call.name for request in permission_gate.requests] == ["read_file"]
    assert not (tmp_path / "new.txt").exists()
    assert (tmp_path / "note.txt").read_text(encoding="utf-8") == "durable evidence\n"

    event_types = [event.event_type for event in snapshot.events]
    assert event_types == [
        SessionEventType.SESSION_CREATED,
        SessionEventType.TURN_STARTED,
        SessionEventType.USER_MESSAGE,
        SessionEventType.CONTEXT_MANIFEST_RECORDED,
        SessionEventType.MODEL_REQUEST_STARTED,
        SessionEventType.MODEL_REQUEST_COMPLETED,
        SessionEventType.ASSISTANT_MESSAGE,
        SessionEventType.TOOL_PROPOSED,
        SessionEventType.TOOL_FAILED,
        SessionEventType.CONTEXT_MANIFEST_RECORDED,
        SessionEventType.MODEL_REQUEST_STARTED,
        SessionEventType.MODEL_REQUEST_COMPLETED,
        SessionEventType.ASSISTANT_MESSAGE,
        SessionEventType.TOOL_PROPOSED,
        SessionEventType.TOOL_VALIDATED,
        SessionEventType.TOOL_STARTED,
        SessionEventType.TOOL_COMPLETED,
        SessionEventType.CONTEXT_MANIFEST_RECORDED,
        SessionEventType.MODEL_REQUEST_STARTED,
        SessionEventType.MODEL_REQUEST_COMPLETED,
        SessionEventType.ASSISTANT_MESSAGE,
        SessionEventType.TURN_COMPLETED,
    ]
    proposed = next(
        event
        for event in snapshot.events
        if event.event_type == SessionEventType.TOOL_PROPOSED
        and event.payload["tool_call_id"] == "call-unknown"
    )
    invalid = next(
        event
        for event in snapshot.events
        if event.event_type == SessionEventType.TOOL_FAILED
        and event.payload["tool_call_id"] == "call-unknown"
    )
    assert invalid.causation_id == proposed.event_id
    assert invalid.payload["outcome"] == ToolOutcome.INVALID.value
    assert invalid.payload["result"]["tool_call_id"] == "call-unknown"
    assert invalid.payload["result"]["error"] == {
        "category": "tool-validation",
        "code": "unknown-tool",
        "message": "Tool name is not registered",
    }

    second_frame = provider.requests[1]
    assert isinstance(second_frame, ContextFrame)
    history = [
        message for message in second_frame.messages if message.layer is ContextLayerName.HISTORY
    ]
    assert isinstance(history[1].message, ToolResultMessage)
    assert history[1].message.tool_call_id == "call-unknown"
    assert history[1].message.outcome == ToolOutcome.INVALID.value


@pytest.mark.asyncio
async def test_known_invalid_tool_still_uses_registered_output_bound(tmp_path: Path) -> None:
    provider = ScriptedFakeModelProvider(
        responses=(
            _tool_response(
                "call-invalid",
                "small_read",
                {"path": ""},
                input_tokens=1,
                output_tokens=1,
            ),
            _text_response("The known Tool input was invalid.", input_tokens=1, output_tokens=1),
        )
    )
    clock = DeterministicClock(datetime(2026, 1, 1, tzinfo=UTC))
    ids = DeterministicIdGenerator()
    store = SessionStore(tmp_path, clock=clock, id_generator=ids)
    application = AgentTurnApplication(
        provider=provider,
        workspace=Workspace(tmp_path),
        tool_registry=ToolRegistry([_SmallBoundReadFileTool()]),
        clock=clock,
        id_generator=ids,
        session_store=store,
    )

    result = await application.run("validate the known Tool")
    snapshot = store.read(result.session_id)

    assert result.tool_results[0].outcome == ToolOutcome.FAILED.value
    terminal = next(
        event
        for event in snapshot.events
        if event.event_type == SessionEventType.TOOL_FAILED
        and event.payload["tool_call_id"] == "call-invalid"
    )
    assert terminal.payload["result"]["error"]["code"] == "output-limit"


def _create_interrupted_unknown_session(tmp_path: Path) -> tuple[SessionStore, str]:
    clock = DeterministicClock(datetime(2026, 1, 1, tzinfo=UTC))
    store = SessionStore(tmp_path, clock=clock, id_generator=DeterministicIdGenerator())
    writer = store.create("recovery-session", created_at=clock.now())
    turn = writer.append(SessionEventType.TURN_STARTED, {}, turn_id="turn-original")
    user = writer.append(
        SessionEventType.USER_MESSAGE,
        {"role": "user", "content": "recover the unknown call"},
        turn_id=turn.turn_id,
        causation_id=turn.event_id,
    )
    request = writer.append(
        SessionEventType.MODEL_REQUEST_STARTED,
        {"request_id": "request-original", "message_count": 1},
        turn_id=turn.turn_id,
        causation_id=user.event_id,
    )
    completed = writer.append(
        SessionEventType.MODEL_REQUEST_COMPLETED,
        {"request_id": "request-original", "input_tokens": 1, "output_tokens": 1},
        turn_id=turn.turn_id,
        causation_id=request.event_id,
    )
    assistant = writer.append(
        SessionEventType.ASSISTANT_MESSAGE,
        {
            "content": "",
            "tool_calls": [
                {
                    "tool_call_id": "call-interrupted-unknown",
                    "name": "missing_tool",
                    "arguments": {"path": "never-created.txt"},
                }
            ],
        },
        turn_id=turn.turn_id,
        causation_id=completed.event_id,
    )
    proposed = writer.append(
        SessionEventType.TOOL_PROPOSED,
        {
            "tool_call_id": "call-interrupted-unknown",
            "name": "missing_tool",
            "arguments": {"path": "never-created.txt"},
        },
        turn_id=turn.turn_id,
        causation_id=assistant.event_id,
    )
    validated = writer.append(
        SessionEventType.TOOL_VALIDATED,
        {
            "tool_call_id": "call-interrupted-unknown",
            "name": "missing_tool",
            "arguments": {"path": "never-created.txt"},
            "risk": {"side_effect": "write", "resources": [], "hazards": [], "summary": "old"},
        },
        turn_id=turn.turn_id,
        causation_id=proposed.event_id,
    )
    writer.append(
        SessionEventType.TOOL_STARTED,
        {
            "tool_call_id": "call-interrupted-unknown",
            "name": "missing_tool",
            "recovery": {"arguments": {"path": "never-created.txt"}},
        },
        turn_id=turn.turn_id,
        causation_id=validated.event_id,
    )
    writer.close()
    return store, "recovery-session"


@pytest.mark.asyncio
async def test_retry_interrupted_unknown_tool_returns_invalid_without_second_lookup(
    tmp_path: Path,
) -> None:
    store, session_id = _create_interrupted_unknown_session(tmp_path)
    registry = _CountingRegistry()
    permission_gate = _RecordingPermissionGate()
    clock = DeterministicClock(datetime(2026, 1, 1, tzinfo=UTC))
    ids = DeterministicIdGenerator()
    application = AgentTurnApplication(
        provider=ScriptedFakeModelProvider(),
        workspace=Workspace(tmp_path),
        tool_registry=registry,
        clock=clock,
        id_generator=ids,
        session_store=store,
        permission_gate=permission_gate,
    )

    result = await application.retry_interrupted(session_id)
    snapshot = store.read(session_id)

    assert result.tool_results[0].outcome is ToolOutcome.INVALID
    assert result.tool_results[0].tool_call_id == result.new_tool_call_ids[0]
    assert result.old_tool_call_ids == ("call-interrupted-unknown",)
    assert registry.require_calls == ["missing_tool"]
    assert permission_gate.requests == []
    assert not (tmp_path / "never-created.txt").exists()
    assert [event.event_type for event in snapshot.events].count(
        SessionEventType.TOOL_INTERRUPTED
    ) == 1
    failed = [
        event
        for event in snapshot.events
        if event.event_type == SessionEventType.TOOL_FAILED
        and event.payload["tool_call_id"] == result.new_tool_call_ids[0]
    ]
    assert len(failed) == 1
    assert failed[0].payload["outcome"] == ToolOutcome.INVALID.value
    assert failed[0].payload["result"]["error"]["category"] == "tool-validation"
    assert failed[0].payload["result"]["error"]["code"] == "unknown-tool"
