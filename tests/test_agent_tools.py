from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic import BaseModel

from mini_agent.adapters.clocks import DeterministicClock
from mini_agent.adapters.ids import DeterministicIdGenerator
from mini_agent.adapters.session_store import SessionStore
from mini_agent.application.agent import AgentTurnApplication
from mini_agent.context import ContextBuilder, ContextFrame, ContextLayerName
from mini_agent.domain.messages import AssistantMessage, ToolResultMessage
from mini_agent.domain.sessions import SessionEventType
from mini_agent.domain.streams import (
    ResponseCompleted,
    ResponseStarted,
    TextDelta,
    ToolCallArgumentDelta,
    ToolCallCompleted,
    ToolCallStarted,
    UsageReported,
)
from mini_agent.providers.fake import ScriptedFakeModelProvider
from mini_agent.tools.contracts import (
    RiskAssessment,
    SideEffectCategory,
    ToolCall,
    ToolLimits,
    ToolOutcome,
    ToolRegistry,
    ToolResult,
)
from mini_agent.tools.files import ReadFileTool, SearchFilesTool
from mini_agent.tools.workspace import Workspace


class _WriteInput(BaseModel):
    path: str


class _WriteProbeTool:
    name = "write_probe"
    description = "Probe a write that must be denied by the read-only gate."
    side_effect = SideEffectCategory.WRITE
    input_model = _WriteInput
    limits = ToolLimits()

    def __init__(self) -> None:
        self.execution_count = 0

    def assess(self, arguments: _WriteInput) -> RiskAssessment:
        return RiskAssessment(
            side_effect=self.side_effect,
            resources=(arguments.path,),
            summary="write a probe file",
        )

    async def execute(self, workspace: Workspace, arguments: _WriteInput) -> ToolResult:
        del workspace, arguments
        self.execution_count += 1
        return ToolResult.succeeded(
            ToolCall(tool_call_id="internal", name=self.name, arguments={}),
            {"executed": True},
        )


def _provider_for_read(*, path: str, final_text: str) -> ScriptedFakeModelProvider:
    return ScriptedFakeModelProvider(
        responses=(
            (
                ResponseStarted(request_id="request-read"),
                ToolCallStarted(tool_call_id="call-read", name="read_file"),
                ToolCallArgumentDelta(
                    tool_call_id="call-read",
                    arguments=json.dumps({"path": path})[:8],
                ),
                ToolCallArgumentDelta(
                    tool_call_id="call-read",
                    arguments=json.dumps({"path": path})[8:],
                ),
                ToolCallCompleted(tool_call_id="call-read"),
                UsageReported(input_tokens=3, output_tokens=4),
                ResponseCompleted(stop_reason="tool_calls"),
            ),
            (
                ResponseStarted(request_id="request-final"),
                TextDelta(text=final_text),
                UsageReported(input_tokens=5, output_tokens=2),
                ResponseCompleted(),
            ),
        )
    )


def _application(
    tmp_path: Path,
    provider: ScriptedFakeModelProvider,
) -> tuple[AgentTurnApplication, SessionStore]:
    clock = DeterministicClock(datetime(2026, 1, 1, tzinfo=UTC))
    ids = DeterministicIdGenerator()
    store = SessionStore(tmp_path, clock=clock, id_generator=ids)
    application = AgentTurnApplication(
        provider=provider,
        workspace=Workspace(tmp_path),
        tool_registry=ToolRegistry([ReadFileTool(), SearchFilesTool()]),
        clock=clock,
        id_generator=ids,
        session_store=store,
    )
    return application, store


@pytest.mark.asyncio
async def test_fake_agent_records_serial_read_lifecycle_and_preserves_tool_pairing(
    tmp_path: Path,
) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "main.py").write_text("print('ok')\n", encoding="utf-8")
    provider = _provider_for_read(path="src/main.py", final_text="Read and verified.")
    application, store = _application(tmp_path, provider)

    result = await application.run("inspect the main file")
    snapshot = store.read(result.session_id)

    assert [event.event_type for event in snapshot.events] == [
        SessionEventType.SESSION_CREATED,
        SessionEventType.TURN_STARTED,
        SessionEventType.USER_MESSAGE,
        SessionEventType.MODEL_REQUEST_STARTED,
        SessionEventType.MODEL_REQUEST_COMPLETED,
        SessionEventType.ASSISTANT_MESSAGE,
        SessionEventType.TOOL_PROPOSED,
        SessionEventType.TOOL_VALIDATED,
        SessionEventType.TOOL_STARTED,
        SessionEventType.TOOL_COMPLETED,
        SessionEventType.MODEL_REQUEST_STARTED,
        SessionEventType.MODEL_REQUEST_COMPLETED,
        SessionEventType.ASSISTANT_MESSAGE,
        SessionEventType.TURN_COMPLETED,
    ]
    assert result.model_request_count == 2
    assert result.tool_call_count == 1
    assert result.tool_results[0].tool_call_id == "call-read"
    assert result.tool_results[0].outcome == ToolOutcome.SUCCESS.value
    assert "print('ok')" in result.tool_results[0].content

    assert snapshot.projection is not None
    assert [message.role for message in snapshot.projection.messages] == [
        "user",
        "assistant",
        "tool",
        "assistant",
    ]
    assert len(snapshot.projection.turns[0].tool_calls) == 1
    assert snapshot.projection.turns[0].tool_calls[0].result is not None
    terminal_events = [
        event
        for event in snapshot.events
        if event.event_type
        in {
            SessionEventType.TOOL_COMPLETED,
            SessionEventType.TOOL_FAILED,
            SessionEventType.TOOL_INTERRUPTED,
        }
    ]
    assert len(terminal_events) == 1
    assert terminal_events[0].payload["tool_call_id"] == "call-read"

    second_request = provider.requests[1]
    assert isinstance(second_request, tuple)
    assert isinstance(second_request[-1], ToolResultMessage)
    assert second_request[-1].tool_call_id == "call-read"


@pytest.mark.asyncio
async def test_fake_agent_returns_bounded_failure_and_model_receives_it(tmp_path: Path) -> None:
    provider = _provider_for_read(path="../outside.txt", final_text="I could not read it.")
    application, store = _application(tmp_path, provider)

    result = await application.run("read outside")
    snapshot = store.read(result.session_id)

    assert result.assistant_message == AssistantMessage("I could not read it.")
    assert len(result.tool_results) == 1
    assert result.tool_results[0].outcome == ToolOutcome.FAILED.value
    assert "outside.txt" not in result.tool_results[0].content
    assert "Workspace traversal" in result.tool_results[0].content
    assert isinstance(provider.requests[1][-1], ToolResultMessage)
    assert provider.requests[1][-1].outcome == ToolOutcome.FAILED.value
    assert [event.event_type for event in snapshot.events].count(
        SessionEventType.TOOL_FAILED
    ) == 1


@pytest.mark.asyncio
async def test_invalid_tool_arguments_have_one_terminal_result_without_starting_execution(
    tmp_path: Path,
) -> None:
    provider = _provider_for_read(path="", final_text="The request was invalid.")
    application, store = _application(tmp_path, provider)

    result = await application.run("read with invalid arguments")
    events = store.read(result.session_id).events

    assert result.tool_results[0].outcome == ToolOutcome.INVALID.value
    assert SessionEventType.TOOL_VALIDATED not in [event.event_type for event in events]
    assert SessionEventType.TOOL_STARTED not in [event.event_type for event in events]
    assert [event.event_type for event in events].count(SessionEventType.TOOL_FAILED) == 1


@pytest.mark.asyncio
async def test_non_read_tool_is_denied_without_ui_prompt_or_execution(tmp_path: Path) -> None:
    provider = ScriptedFakeModelProvider(
        responses=(
            (
                ResponseStarted(request_id="request-write"),
                ToolCallStarted(tool_call_id="call-write", name="write_probe"),
                ToolCallArgumentDelta(
                    tool_call_id="call-write",
                    arguments=json.dumps({"path": "new.txt"}),
                ),
                ToolCallCompleted(tool_call_id="call-write"),
                ResponseCompleted(stop_reason="tool_calls"),
            ),
            (
                ResponseStarted(request_id="request-denied"),
                TextDelta(text="The write was denied."),
                ResponseCompleted(),
            ),
        )
    )
    write_tool = _WriteProbeTool()
    clock = DeterministicClock(datetime(2026, 1, 1, tzinfo=UTC))
    ids = DeterministicIdGenerator()
    store = SessionStore(tmp_path, clock=clock, id_generator=ids)
    application = AgentTurnApplication(
        provider=provider,
        workspace=Workspace(tmp_path),
        tool_registry=ToolRegistry([write_tool]),
        clock=clock,
        id_generator=ids,
        session_store=store,
    )

    result = await application.run("write a file")
    events = store.read(result.session_id).events

    assert result.tool_results[0].tool_call_id == "call-write"
    assert result.tool_results[0].outcome == ToolOutcome.DENIED.value
    assert write_tool.execution_count == 0
    assert SessionEventType.TOOL_PROPOSED in [event.event_type for event in events]
    assert SessionEventType.TOOL_VALIDATED in [event.event_type for event in events]
    assert SessionEventType.TOOL_STARTED not in [event.event_type for event in events]
    assert [event.event_type for event in events].count(SessionEventType.TOOL_FAILED) == 1


@pytest.mark.asyncio
async def test_context_builder_preserves_structured_tool_pairing_for_next_request(
    tmp_path: Path,
) -> None:
    (tmp_path / "main.py").write_text("print('ok')\n", encoding="utf-8")
    provider = _provider_for_read(path="main.py", final_text="Done.")
    clock = DeterministicClock(datetime(2026, 1, 1, tzinfo=UTC))
    ids = DeterministicIdGenerator()
    store = SessionStore(tmp_path, clock=clock, id_generator=ids)
    application = AgentTurnApplication(
        provider=provider,
        workspace=Workspace(tmp_path),
        tool_registry=ToolRegistry([ReadFileTool(), SearchFilesTool()]),
        clock=clock,
        id_generator=ids,
        session_store=store,
        context_builder=ContextBuilder(tmp_path),
    )

    await application.run("inspect the file")

    second_frame = provider.requests[1]
    assert isinstance(second_frame, ContextFrame)
    history = [
        message
        for message in second_frame.messages
        if message.layer is ContextLayerName.HISTORY
    ]
    assert [message.role for message in history[:3]] == ["user", "assistant", "tool"]
    assert '"tool_call_id": "call-read"' in history[1].content
    assert '"name": "read_file"' in history[1].content
    assert "print('ok')" in history[2].content
