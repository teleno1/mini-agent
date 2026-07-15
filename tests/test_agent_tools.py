from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic import BaseModel

from mini_agent.adapters.clocks import DeterministicClock
from mini_agent.adapters.ids import DeterministicIdGenerator
from mini_agent.adapters.session_store import SessionStore
from mini_agent.application.agent import AgentLimitError, AgentTurnApplication
from mini_agent.configuration import EffectiveConfiguration, PermissionMode
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


class _InterruptProbeTool:
    name = "interrupt_probe"
    description = "Probe an operation whose termination is uncertain."
    side_effect = SideEffectCategory.READ
    input_model = _WriteInput
    limits = ToolLimits()

    def __init__(self, started: asyncio.Event) -> None:
        self.started = started

    def assess(self, arguments: _WriteInput) -> RiskAssessment:
        return RiskAssessment(
            side_effect=self.side_effect,
            resources=(arguments.path,),
            summary="wait for an interrupt",
        )

    async def execute(self, workspace: Workspace, arguments: _WriteInput) -> ToolResult:
        del workspace, arguments
        self.started.set()
        await asyncio.Event().wait()
        raise AssertionError("the interrupted Tool should not return normally")


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


def _provider_for_search(*, final_text: str) -> ScriptedFakeModelProvider:
    return ScriptedFakeModelProvider(
        scripts=(
            (
                ResponseStarted(request_id="request-search"),
                ToolCallStarted(tool_call_id="call-search", name="search_files"),
                ToolCallArgumentDelta(
                    tool_call_id="call-search",
                    arguments=json.dumps({"query": "needle", "directory": "src", "glob": "*.py"}),
                ),
                ToolCallCompleted(tool_call_id="call-search"),
                ResponseCompleted(stop_reason="tool_calls"),
            ),
            (
                ResponseStarted(request_id="request-search-final"),
                TextDelta(text=final_text),
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
    validated_event = next(
        event for event in snapshot.events if event.event_type == SessionEventType.TOOL_VALIDATED
    )
    permission = validated_event.payload["permission"]
    assert permission["tool_call_id"] == "call-read"
    assert permission["decision"] == "allow"
    assert permission["matched_rule"] == "safe-read"
    assert len(permission["argument_hash"]) == 64
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
    assert [event.event_type for event in snapshot.events].count(SessionEventType.TOOL_FAILED) == 1
    assert SessionEventType.TOOL_STARTED not in [event.event_type for event in snapshot.events]


@pytest.mark.asyncio
async def test_fake_agent_adapts_to_bounded_search_results(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "main.py").write_text("needle = True\n", encoding="utf-8")
    provider = _provider_for_search(final_text="Search complete.")
    application, _ = _application(tmp_path, provider)

    result = await application.run("find needle")

    assert result.assistant_message.content == "Search complete."
    assert result.tool_results[0].tool_call_id == "call-search"
    assert result.tool_results[0].outcome == ToolOutcome.SUCCESS.value
    assert "src/main.py" in result.tool_results[0].content
    assert isinstance(provider.requests[1][-1], ToolResultMessage)
    assert provider.requests[1][-1].tool_call_id == "call-search"


@pytest.mark.asyncio
async def test_agent_fails_before_provider_work_when_active_budget_is_exhausted(
    tmp_path: Path,
) -> None:
    provider = ScriptedFakeModelProvider()
    clock = DeterministicClock(datetime(2026, 1, 1, tzinfo=UTC))
    ids = DeterministicIdGenerator()
    store = SessionStore(tmp_path, clock=clock, id_generator=ids)
    configuration = EffectiveConfiguration(
        model="fake",
        permission_mode=PermissionMode.SUGGEST,
        provider_base_url="https://example.test/v1",
        max_model_requests=25,
        max_tool_calls=50,
        max_active_seconds=0,
        context_window_tokens=1000,
        response_reserve_tokens=100,
        artifact_threshold_bytes=32 * 1024,
        instruction_file_bytes=32 * 1024,
        instruction_chain_bytes=128 * 1024,
    )
    application = AgentTurnApplication(
        provider=provider,
        workspace=Workspace(tmp_path),
        tool_registry=ToolRegistry([ReadFileTool(), SearchFilesTool()]),
        clock=clock,
        id_generator=ids,
        session_store=store,
        configuration=configuration,
    )

    with pytest.raises(AgentLimitError, match="active execution budget"):
        await application.run("do no work")

    snapshot = store.read("session-0001")
    assert [event.event_type for event in snapshot.events].count(SessionEventType.TURN_FAILED) == 1
    assert provider.requests == []


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
async def test_cancelled_started_tool_has_one_interrupted_terminal_event(tmp_path: Path) -> None:
    provider = ScriptedFakeModelProvider(
        responses=(
            (
                ResponseStarted(request_id="request-interrupt"),
                ToolCallStarted(tool_call_id="call-interrupt", name="interrupt_probe"),
                ToolCallArgumentDelta(
                    tool_call_id="call-interrupt",
                    arguments=json.dumps({"path": "main.py"}),
                ),
                ToolCallCompleted(tool_call_id="call-interrupt"),
                ResponseCompleted(stop_reason="tool_calls"),
            ),
        )
    )
    started = asyncio.Event()
    clock = DeterministicClock(datetime(2026, 1, 1, tzinfo=UTC))
    ids = DeterministicIdGenerator()
    store = SessionStore(tmp_path, clock=clock, id_generator=ids)
    application = AgentTurnApplication(
        provider=provider,
        workspace=Workspace(tmp_path),
        tool_registry=ToolRegistry([_InterruptProbeTool(started)]),
        clock=clock,
        id_generator=ids,
        session_store=store,
    )

    task = asyncio.create_task(application.run("interrupt the read"))
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    snapshot = store.read("session-0001")
    events = [event.event_type for event in snapshot.events]
    assert events.count(SessionEventType.TOOL_INTERRUPTED) == 1
    assert events.count(SessionEventType.TOOL_COMPLETED) == 0
    assert events.count(SessionEventType.TOOL_FAILED) == 0
    assert events.count(SessionEventType.TURN_FAILED) == 1
    assert snapshot.projection is not None
    assert snapshot.projection.turns[0].tool_calls[0].status == "interrupted"


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
        message for message in second_frame.messages if message.layer is ContextLayerName.HISTORY
    ]
    current_user = [
        message
        for message in second_frame.messages
        if message.layer is ContextLayerName.CURRENT_USER
    ]
    assert [message.content for message in current_user] == ["inspect the file"]
    assert [message.role for message in history[:2]] == ["assistant", "tool"]
    assert '"tool_call_id": "call-read"' in history[0].content
    assert '"name": "read_file"' in history[0].content
    assert "print('ok')" in history[1].content
