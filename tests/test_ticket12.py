from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic import BaseModel
from typer.testing import CliRunner

from mini_agent.adapters.clocks import DeterministicClock
from mini_agent.adapters.ids import DeterministicIdGenerator
from mini_agent.adapters.session_store import SessionPersistenceError, SessionStore
from mini_agent.application.agent import AgentTurnApplication, TurnBudgets
from mini_agent.application.cancellation import ForcedInterrupt, InterruptController
from mini_agent.application.turns import TextTurnApplication
from mini_agent.cli.app import app
from mini_agent.diagnostics import DiagnosticLogger
from mini_agent.domain.sessions import SessionEventType
from mini_agent.domain.streams import (
    Failure,
    FailureCategory,
    ResponseCompleted,
    ResponseFailed,
    ResponseStarted,
    TextDelta,
    ToolCallCompleted,
    ToolCallStarted,
)
from mini_agent.providers.fake import ScriptedFakeModelProvider
from mini_agent.tools.contracts import (
    PermissionDecision,
    PermissionRequest,
    RiskAssessment,
    SideEffectCategory,
    ToolLimits,
    ToolRegistry,
    ToolResult,
)
from mini_agent.tools.files import ReadFileTool
from mini_agent.tools.workspace import Workspace


def _failure(*, retryable: bool = True, retry_after: float | None = None) -> ResponseFailed:
    return ResponseFailed(
        Failure(
            category=FailureCategory.NETWORK.value,
            source="fake-provider",
            redacted_description="temporary upstream failure",
            retryable=retryable,
            required_user_action="retry",
            code="temporary-network",
            retry_after_seconds=retry_after,
        )
    )


def _clock_ids_store(
    tmp_path: Path,
) -> tuple[DeterministicClock, DeterministicIdGenerator, SessionStore]:
    clock = DeterministicClock(datetime(2026, 1, 1, tzinfo=UTC))
    ids = DeterministicIdGenerator()
    return clock, ids, SessionStore(tmp_path, clock=clock, id_generator=ids)


@pytest.mark.asyncio
async def test_retry_budget_is_three_total_requests_with_new_request_ids(tmp_path: Path) -> None:
    provider = ScriptedFakeModelProvider(
        responses=(
            (ResponseStarted(request_id="provider-1"), _failure()),
            (ResponseStarted(request_id="provider-2"), _failure()),
            (ResponseStarted(request_id="provider-3"), _failure()),
        )
    )
    clock, ids, store = _clock_ids_store(tmp_path)
    application = AgentTurnApplication(
        provider=provider,
        workspace=Workspace(tmp_path),
        tool_registry=ToolRegistry([ReadFileTool()]),
        clock=clock,
        id_generator=ids,
        session_store=store,
        retry_sleep=lambda delay: asyncio.sleep(0),
    )

    with pytest.raises(RuntimeError, match="temporary upstream failure"):
        await application.run("retry three times")

    events = store.read("session-0001").events
    request_starts = [
        event.payload["request_id"]
        for event in events
        if event.event_type == SessionEventType.MODEL_REQUEST_STARTED
    ]
    assert len(request_starts) == 3
    assert len(set(request_starts)) == 3
    assert len(provider.requests) == 3
    assert events[-1].event_type == SessionEventType.TURN_FAILED


@pytest.mark.asyncio
async def test_retry_after_is_used_and_partial_stream_is_never_retried(tmp_path: Path) -> None:
    delays: list[float] = []
    provider = ScriptedFakeModelProvider(
        responses=(
            (ResponseStarted(request_id="provider-1"), _failure(retry_after=4.5)),
            (ResponseStarted(request_id="provider-2"), TextDelta("done"), ResponseCompleted()),
        )
    )
    clock, ids, store = _clock_ids_store(tmp_path)

    async def capture_delay(delay: float) -> None:
        delays.append(delay)

    application = AgentTurnApplication(
        provider=provider,
        workspace=Workspace(tmp_path),
        tool_registry=ToolRegistry([ReadFileTool()]),
        clock=clock,
        id_generator=ids,
        session_store=store,
        budgets=TurnBudgets(max_retries=1),
        retry_sleep=capture_delay,
    )

    result = await application.run("retry before output")
    assert result.assistant_message.content == "done"
    assert delays == [4.5]

    partial = ScriptedFakeModelProvider(
        responses=(
            (
                ResponseStarted(request_id="partial"),
                TextDelta("already visible"),
                _failure(),
            ),
        )
    )
    partial_app = AgentTurnApplication(
        provider=partial,
        workspace=Workspace(tmp_path),
        tool_registry=ToolRegistry([ReadFileTool()]),
        clock=clock,
        id_generator=ids,
        session_store=store,
        budgets=TurnBudgets(max_retries=2),
    )
    with pytest.raises(RuntimeError, match="temporary upstream failure"):
        await partial_app.run("do not replay partial output")
    assert len(partial.requests) == 1


@pytest.mark.asyncio
async def test_text_turn_also_retries_only_before_output(tmp_path: Path) -> None:
    provider = ScriptedFakeModelProvider(
        responses=(
            (ResponseStarted(request_id="text-1"), _failure()),
            (ResponseStarted(request_id="text-2"), TextDelta("recovered"), ResponseCompleted()),
        )
    )
    clock, ids, store = _clock_ids_store(tmp_path)
    application = TextTurnApplication(
        provider=provider,
        clock=clock,
        id_generator=ids,
        session_store=store,
        max_retries=1,
        retry_sleep=lambda delay: asyncio.sleep(0),
    )

    result = await application.run("retry text")
    events = store.read(result.session_id).events
    assert result.assistant_message.content == "recovered"
    assert len(provider.requests) == 2
    assert [
        event.payload["request_id"]
        for event in events
        if event.event_type == SessionEventType.MODEL_REQUEST_STARTED
    ] == ["request-0001", "request-0002"]


@pytest.mark.asyncio
async def test_cancellation_during_streaming_closes_request_and_turn(tmp_path: Path) -> None:
    started = asyncio.Event()
    release = asyncio.Event()

    class BlockingProvider:
        def stream(self, messages):
            return self._stream()

        async def _stream(self):
            yield ResponseStarted(request_id="blocking")
            started.set()
            await release.wait()
            yield TextDelta("never durable")
            yield ResponseCompleted()

    clock, ids, store = _clock_ids_store(tmp_path)
    application = AgentTurnApplication(
        provider=BlockingProvider(),
        workspace=Workspace(tmp_path),
        tool_registry=ToolRegistry([ReadFileTool()]),
        clock=clock,
        id_generator=ids,
        session_store=store,
    )
    task = asyncio.create_task(application.run("cancel streaming"))
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    snapshot = store.read("session-0001")
    assert snapshot.projection is not None
    assert snapshot.projection.status.value == "failed"
    events = [event.event_type for event in snapshot.events]
    assert events.count(SessionEventType.MODEL_REQUEST_FAILED) == 1
    assert events.count(SessionEventType.TURN_FAILED) == 1
    assert SessionEventType.TURN_COMPLETED not in events
    assert snapshot.resumable


@pytest.mark.asyncio
async def test_async_permission_cancellation_has_one_terminal_tool_result(tmp_path: Path) -> None:
    permission_started = asyncio.Event()
    permission_release = asyncio.Event()

    class AsyncPermission:
        def decide(self, request: PermissionRequest):
            del request
            permission_started.set()
            return self._wait()

        async def _wait(self):
            await permission_release.wait()
            return PermissionDecision.ALLOW

    provider = ScriptedFakeModelProvider(
        responses=(
            (
                ResponseStarted(request_id="permission"),
                ToolCallStarted(tool_call_id="read-1", name="read_file"),
                ToolCallCompleted(tool_call_id="read-1", arguments={"path": "main.py"}),
                ResponseCompleted(stop_reason="tool_calls"),
            ),
        )
    )
    (tmp_path / "main.py").write_text("safe\n", encoding="utf-8")
    clock, ids, store = _clock_ids_store(tmp_path)
    application = AgentTurnApplication(
        provider=provider,
        workspace=Workspace(tmp_path),
        tool_registry=ToolRegistry([ReadFileTool()]),
        clock=clock,
        id_generator=ids,
        session_store=store,
        permission_gate=AsyncPermission(),
    )
    task = asyncio.create_task(application.run("cancel permission"))
    await permission_started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    events = store.read("session-0001").events
    terminal = [
        event
        for event in events
        if event.event_type
        in {
            SessionEventType.TOOL_COMPLETED,
            SessionEventType.TOOL_FAILED,
            SessionEventType.TOOL_INTERRUPTED,
        }
    ]
    assert len(terminal) == 1
    assert terminal[0].payload["outcome"] == "cancelled"
    assert terminal[0].event_type == SessionEventType.TOOL_FAILED


@pytest.mark.asyncio
async def test_started_tool_timeout_is_interrupted_and_not_retried(tmp_path: Path) -> None:
    class SlowInput(BaseModel):
        value: str

    class SlowTool:
        name = "slow"
        description = "A deliberately bounded slow Tool."
        side_effect = SideEffectCategory.EXECUTE
        input_model = SlowInput
        limits = ToolLimits(timeout_seconds=0.01)

        def assess(self, arguments: SlowInput) -> RiskAssessment:
            del arguments
            return RiskAssessment(
                side_effect=self.side_effect,
                summary="run a slow operation",
            )

        async def execute(self, workspace: Workspace, arguments: SlowInput) -> ToolResult:
            del workspace, arguments
            await asyncio.sleep(1)
            return ToolResult(
                tool_call_id="placeholder",
                tool_name=self.name,
                outcome="success",
            )

    class AllowPermission:
        def decide(self, request: PermissionRequest) -> PermissionDecision:
            del request
            return PermissionDecision.ALLOW

    provider = ScriptedFakeModelProvider(
        responses=(
            (
                ResponseStarted(request_id="timeout"),
                ToolCallStarted(tool_call_id="slow-1", name="slow"),
                ToolCallCompleted(tool_call_id="slow-1", arguments={"value": "x"}),
                ResponseCompleted(stop_reason="tool_calls"),
            ),
        )
    )
    clock, ids, store = _clock_ids_store(tmp_path)
    application = AgentTurnApplication(
        provider=provider,
        workspace=Workspace(tmp_path),
        tool_registry=ToolRegistry([SlowTool()]),
        clock=clock,
        id_generator=ids,
        session_store=store,
        permission_gate=AllowPermission(),
    )

    with pytest.raises(asyncio.CancelledError):
        await application.run("timeout safely")

    events = store.read("session-0001").events
    terminal = [event for event in events if event.event_type == SessionEventType.TOOL_INTERRUPTED]
    assert len(terminal) == 1
    assert terminal[0].payload["outcome"] == "interrupted"


@pytest.mark.asyncio
async def test_broken_stdout_observer_does_not_change_durable_success(tmp_path: Path) -> None:
    provider = ScriptedFakeModelProvider(chunks=("answer",))
    clock, ids, store = _clock_ids_store(tmp_path)
    application = AgentTurnApplication(
        provider=provider,
        workspace=Workspace(tmp_path),
        tool_registry=ToolRegistry([ReadFileTool()]),
        clock=clock,
        id_generator=ids,
        session_store=store,
    )

    def broken_stdout(event) -> None:
        del event
        raise OSError("stdout closed")

    result = await application.run("keep going", on_event=broken_stdout)
    assert result.assistant_message.content == "answer"
    assert store.read(result.session_id).events[-1].event_type == SessionEventType.TURN_COMPLETED


def test_failure_taxonomy_and_diagnostic_lookup_are_redacted(tmp_path: Path) -> None:
    clock = DeterministicClock(datetime(2026, 1, 1, tzinfo=UTC))
    ids = DeterministicIdGenerator()
    logger = DiagnosticLogger(tmp_path, id_generator=ids, max_files=2, max_bytes=4096)
    failure = logger.record(
        Failure(
            category=FailureCategory.PERSISTENCE.value,
            source="session-store",
            redacted_description="could not persist api_key=sk-ticket12-secret",
            retryable=False,
            required_user_action="run doctor",
            cause="Bearer sk-ticket12-secret",
            details={"prompt": "do not persist this", "safe": "value"},
        ),
        session_id="session-1",
        turn_id="turn-1",
        request_id="request-1",
        tool_call_id="call-1",
        timestamp=clock.now(),
    )

    assert failure.failure_id == "failure-0001"
    record = logger.find(failure.failure_id or "")
    assert record is not None
    encoded = json.dumps(record, ensure_ascii=False)
    assert "sk-ticket12-secret" not in encoded
    assert record["error_id"] == failure.failure_id
    payload = record["failure"]
    assert isinstance(payload, dict)
    assert payload["session_id"] == "session-1"
    assert payload["request_id"] == "request-1"
    doctor_result = CliRunner().invoke(
        app,
        ["--workspace", str(tmp_path), "doctor", failure.failure_id or ""],
    )
    assert doctor_result.exit_code == 0
    assert failure.failure_id in doctor_result.stdout


@pytest.mark.asyncio
async def test_first_interrupt_acknowledges_and_second_marks_forced_exit() -> None:
    async def wait_forever() -> None:
        await asyncio.Event().wait()

    task = asyncio.create_task(wait_forever())
    controller = InterruptController(task)
    controller.request_interrupt()
    assert controller.acknowledged is True
    assert controller.interrupt_count == 1
    assert controller.cleanup_seconds == 5.0
    with pytest.raises(asyncio.CancelledError):
        await task

    controller.request_interrupt()
    assert controller.forced is True
    assert ForcedInterrupt.exit_code == 130


def test_fsync_failure_is_a_persistence_failure_not_success(tmp_path: Path, monkeypatch) -> None:
    import mini_agent.adapters.session_store as session_store_module

    def fail_fsync(fd: int) -> None:
        del fd
        raise OSError("fsync unavailable")

    monkeypatch.setattr(session_store_module.os, "fsync", fail_fsync)
    clock, ids, store = _clock_ids_store(tmp_path)
    provider = ScriptedFakeModelProvider()
    application = AgentTurnApplication(
        provider=provider,
        workspace=Workspace(tmp_path),
        tool_registry=ToolRegistry([ReadFileTool()]),
        clock=clock,
        id_generator=ids,
        session_store=store,
    )

    with pytest.raises(SessionPersistenceError):
        asyncio.run(application.run("persist safely"))
    event_files = list((tmp_path / ".mini-agent" / "sessions").rglob("events.jsonl"))
    assert event_files and all(path.stat().st_size == 0 for path in event_files)


def test_cli_configuration_failure_uses_exit_code_two(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    config = tmp_path / ".mini-agent"
    config.mkdir()
    (config / "config.toml").write_text("unknown = true\n", encoding="utf-8")

    result = CliRunner().invoke(app, ["configuration fails"])

    assert result.exit_code == 2
