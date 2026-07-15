from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from mini_agent.adapters.clocks import DeterministicClock
from mini_agent.adapters.ids import DeterministicIdGenerator
from mini_agent.adapters.session_store import SessionStore
from mini_agent.application.agent import AgentTurnApplication
from mini_agent.configuration import EffectiveConfiguration, PermissionMode
from mini_agent.context import ContextBuilder
from mini_agent.domain.artifacts import ArtifactReference
from mini_agent.domain.compaction import (
    ContextCompactionError,
    ContextCompactor,
    ContextSummary,
    SummaryValidationError,
    TokenEstimator,
    response_reserve_tokens,
)
from mini_agent.domain.messages import AssistantMessage, ToolCallBlock, ToolResultMessage
from mini_agent.domain.sessions import SessionEventType
from mini_agent.providers.fake import ScriptedFakeModelProvider
from mini_agent.tools.contracts import ToolRegistry
from mini_agent.tools.workspace import Workspace


def _configuration(window: int = 700, reserve: int = 100) -> EffectiveConfiguration:
    return EffectiveConfiguration(
        model="fake",
        permission_mode=PermissionMode.SUGGEST,
        provider_base_url="https://example.test/v1",
        max_model_requests=25,
        max_tool_calls=50,
        max_active_seconds=1800,
        context_window_tokens=window,
        response_reserve_tokens=reserve,
        artifact_threshold_bytes=32 * 1024,
        instruction_file_bytes=32 * 1024,
        instruction_chain_bytes=128 * 1024,
    )


def _summary(boundary: int = 3) -> dict[str, object]:
    return {
        "schema_version": 1,
        "objective": "preserve the observable task",
        "constraints": ["stay in the Workspace"],
        "decisions": ["use the recorded result"],
        "plan": {},
        "files": [],
        "commands_results": [],
        "failures": [],
        "unresolved_work": [],
        "next_actions": ["continue"],
        "references": [],
        "summary_boundary": boundary,
    }


def test_response_reserve_edges_and_provider_calibration() -> None:
    assert response_reserve_tokens(128_000) == 25_600
    assert response_reserve_tokens(1_000_000) == 200_000
    assert response_reserve_tokens(1_000) == 300
    assert response_reserve_tokens(1_000, 100) == 100

    estimator = TokenEstimator()
    before = estimator.estimate_text("a" * 400)
    estimator.calibrate(before, before + 20)
    assert estimator.calibration_samples == 1
    assert estimator.estimate_text("a" * 400) >= before + 20


def test_summary_validates_evidence_artifact_references_and_boundary() -> None:
    artifact = ArtifactReference(
        artifact_id="artifact-0001",
        path="artifacts/artifact-0001.artifact",
        media_type="text/plain",
        byte_count=4,
        sha256="0" * 64,
        preview="data",
        truncated=False,
    )
    candidate = _summary()
    candidate["references"] = [
        {"kind": "event", "sequence": 1, "event_id": "event-1"},
        {
            "kind": "artifact",
            "artifact_id": artifact.artifact_id,
            "path": artifact.path,
            "sha256": artifact.sha256,
            "byte_count": artifact.byte_count,
        },
    ]
    summary = ContextSummary.from_dict(
        candidate,
        events=(
            {"sequence": 1, "event_id": "event-1", "event_type": "tool.completed"},
            {"sequence": 3, "event_id": "event-3", "event_type": "context.compacted"},
        ),
        artifacts=(artifact,),
    )
    assert summary.summary_boundary == 3

    invalid = dict(candidate)
    invalid["summary_boundary"] = 1
    invalid["references"] = [{"kind": "event", "sequence": 2, "event_id": "fake"}]
    with pytest.raises(SummaryValidationError, match="unknown event"):
        ContextSummary.from_dict(
            invalid,
            events=({"sequence": 1, "event_id": "event-1", "event_type": "tool.completed"},),
        )
    with pytest.raises(SummaryValidationError, match="monotonic"):
        ContextSummary.from_dict(_summary(2), previous_boundary=3)


def test_micro_compaction_keeps_tool_call_result_pair_and_artifact_identity() -> None:
    artifact_content = json.dumps(
        {
            "artifact": {
                "artifact_id": "artifact-0001",
                "path": "artifacts/artifact-0001.artifact",
                "media_type": "text/plain",
                "byte_count": 8_000,
                "sha256": "0" * 64,
                "preview": "x" * 3_000,
                "truncated": True,
            }
        },
        separators=(",", ":"),
    )
    history = (
        AssistantMessage("old observation"),
        AssistantMessage(
            "inspect",
            (ToolCallBlock("call-1", "read_file", {"path": "main.py"}),),
        ),
        ToolResultMessage("call-1", artifact_content, "success"),
        AssistantMessage("recent observation"),
    )
    result = ContextCompactor(recent_message_count=2).compact("inspect", history)
    paired = [message for message in result.history if isinstance(message, ToolResultMessage)]
    assert any(message.tool_call_id == "call-1" for message in paired)
    assert any(
        isinstance(message, AssistantMessage)
        and any(call.tool_call_id == "call-1" for call in message.tool_calls)
        for message in result.history
    )
    assert len(paired[0].content) < 3004
    compacted_artifact = json.loads(paired[0].content)["artifact"]
    assert compacted_artifact["artifact_id"] == "artifact-0001"
    assert compacted_artifact["sha256"] == "0" * 64


def test_old_summary_recompression_keeps_facts_and_moves_boundary() -> None:
    compactor = ContextCompactor()
    first = compactor.compact("objective", (AssistantMessage("recorded decision"),))
    second = compactor.compact(
        "objective",
        (AssistantMessage("new observation"),),
        events=({"sequence": 4, "event_id": "event-4", "event_type": "assistant.message"},),
        existing_summary=first.summary,
        summary_boundary=first.summary_boundary,
    )
    assert second.summary is not None
    assert second.summary.summary_boundary == 4
    assert "recorded decision" in second.summary.decisions


def test_fact_summary_preserves_changes_commands_and_failures() -> None:
    events = (
        {
            "sequence": 1,
            "event_id": "event-1",
            "event_type": "tool.proposed",
            "payload": {
                "tool_call_id": "call-shell",
                "arguments": {"command": "pytest -q"},
            },
        },
        {
            "sequence": 2,
            "event_id": "event-2",
            "event_type": "tool.completed",
            "payload": {
                "tool_call_id": "call-shell",
                "name": "shell",
                "outcome": "success",
                "result": {"data": {"changed_files": ["src/main.py"]}},
                "result_text": "passed",
            },
        },
        {
            "sequence": 3,
            "event_id": "event-3",
            "event_type": "tool.failed",
            "payload": {
                "tool_call_id": "call-test",
                "name": "shell",
                "outcome": "failed",
                "result_text": "verification failed",
            },
        },
    )
    result = ContextCompactor().compact("ship the change", (), events=events)
    assert result.summary is not None
    assert {item["path"] for item in result.summary.files} == {"src/main.py"}
    assert result.summary.commands_results[0]["command"] == "pytest -q"
    assert any("verification failed" in failure for failure in result.summary.failures)
    assert result.summary.next_actions


@pytest.mark.asyncio
async def test_long_session_compacts_without_deleting_events(tmp_path: Path) -> None:
    clock = DeterministicClock(datetime(2026, 1, 1, tzinfo=UTC))
    ids = DeterministicIdGenerator()
    store = SessionStore(tmp_path, clock=clock, id_generator=ids)
    provider = ScriptedFakeModelProvider(chunks=("observable answer " * 8,))
    application = AgentTurnApplication(
        provider=provider,
        workspace=Workspace(tmp_path),
        tool_registry=ToolRegistry([]),
        clock=clock,
        id_generator=ids,
        session_store=store,
        context_builder=ContextBuilder(tmp_path, _configuration()),
        configuration=_configuration(),
        context_compactor=ContextCompactor(recent_message_count=20),
    )

    first = await application.run("objective 0")
    for index in range(1, 20):
        await application.run(f"objective {index}", session_id=first.session_id)

    snapshot = store.read(first.session_id)
    assert snapshot.projection is not None
    assert snapshot.projection.context_summary is not None
    assert snapshot.projection.summary_boundary > 0
    summary = snapshot.projection.context_summary
    assert summary.objective
    assert summary.constraints
    assert summary.next_actions
    event_types = [event.event_type for event in snapshot.events]
    assert SessionEventType.CONTEXT_COMPACTION_STARTED in event_types
    assert SessionEventType.CONTEXT_COMPACTION_COMPLETED in event_types
    assert event_types.count(SessionEventType.USER_MESSAGE) == 20
    assert len(provider.requests) == 20


@pytest.mark.asyncio
async def test_three_unsuccessful_compactions_fail_before_provider_request(tmp_path: Path) -> None:
    clock = DeterministicClock(datetime(2026, 1, 1, tzinfo=UTC))
    ids = DeterministicIdGenerator()
    store = SessionStore(tmp_path, clock=clock, id_generator=ids)
    provider = ScriptedFakeModelProvider(chunks=("must not be requested",))
    tiny = _configuration(window=100, reserve=30)
    application = AgentTurnApplication(
        provider=provider,
        workspace=Workspace(tmp_path),
        tool_registry=ToolRegistry([]),
        clock=clock,
        id_generator=ids,
        session_store=store,
        context_builder=ContextBuilder(tmp_path, tiny),
        configuration=tiny,
    )

    with pytest.raises(ContextCompactionError, match="three compaction attempts"):
        await application.run("cannot fit")

    snapshot = store.read("session-0001")
    event_types = [event.event_type for event in snapshot.events]
    assert event_types.count(SessionEventType.CONTEXT_COMPACTION_STARTED) == 3
    assert SessionEventType.CONTEXT_COMPACTION_FAILED in event_types
    assert SessionEventType.MODEL_REQUEST_STARTED not in event_types
    assert event_types[-1] == SessionEventType.TURN_FAILED
    assert provider.requests == []


@pytest.mark.asyncio
async def test_compaction_recovers_after_one_summary_generation_failure(tmp_path: Path) -> None:
    attempts = 0

    def flaky_source(
        objective: str,
        history: tuple[object, ...],
        events: tuple[object, ...],
        plan: object,
        artifacts: tuple[ArtifactReference, ...],
        boundary: int,
    ) -> dict[str, object]:
        del history, events, plan, artifacts
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise ValueError("temporary summary generation failure")
        candidate = _summary(boundary)
        candidate["objective"] = objective
        return candidate

    clock = DeterministicClock(datetime(2026, 1, 1, tzinfo=UTC))
    ids = DeterministicIdGenerator()
    store = SessionStore(tmp_path, clock=clock, id_generator=ids)
    provider = ScriptedFakeModelProvider(chunks=("observable answer " * 8,))
    configuration = _configuration()
    application = AgentTurnApplication(
        provider=provider,
        workspace=Workspace(tmp_path),
        tool_registry=ToolRegistry([]),
        clock=clock,
        id_generator=ids,
        session_store=store,
        context_builder=ContextBuilder(tmp_path, configuration),
        configuration=configuration,
        context_compactor=ContextCompactor(
            recent_message_count=20,
            summary_source=flaky_source,
        ),
    )

    first = await application.run("objective 0")
    for index in range(1, 12):
        await application.run(f"objective {index}", session_id=first.session_id)

    snapshot = store.read(first.session_id)
    event_types = [event.event_type for event in snapshot.events]
    assert attempts >= 2
    assert SessionEventType.CONTEXT_COMPACTION_FAILED in event_types
    assert snapshot.projection is not None
    assert snapshot.projection.context_summary is not None
