"""Controlled interruption and Resume trial harness for RL-02.

The harness deliberately uses deterministic Providers and a fresh Workspace
per trial.  It exercises the public Agent application and JSONL Session Store
without making a real-model request or changing the product source.
"""

from __future__ import annotations

import argparse
import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from mini_agent.adapters.clocks import DeterministicClock
from mini_agent.adapters.ids import DeterministicIdGenerator
from mini_agent.adapters.session_store import SessionStore
from mini_agent.application.agent import AgentTurnApplication
from mini_agent.domain.sessions import SessionEventType
from mini_agent.domain.streams import (
    Failure,
    ResponseFailed,
    ResponseStarted,
    TextDelta,
)
from mini_agent.providers.fake import ScriptedFakeModelProvider
from mini_agent.tools.contracts import PermissionDecision, ToolRegistry
from mini_agent.tools.shell import ShellTool
from mini_agent.tools.workspace import Workspace


def _clock() -> DeterministicClock:
    return DeterministicClock(datetime(2026, 7, 19, tzinfo=UTC))


def _event_records(store: SessionStore, session_id: str) -> list[dict[str, Any]]:
    return [event.to_record() for event in store.read(session_id).events]


def _event_types(records: list[dict[str, Any]]) -> list[str]:
    return [str(record["event_type"]) for record in records]


async def _provider_interruption(root: Path) -> dict[str, Any]:
    root.mkdir(parents=True, exist_ok=True)
    failure = Failure(
        category="network",
        code="controlled-interruption",
        source="provider",
        redacted_description="controlled Provider interruption",
        retryable=False,
        required_user_action="inspect the incomplete Turn and retry later",
    )
    provider = ScriptedFakeModelProvider(
        responses=(
            (
                ResponseStarted(request_id="response-interrupted"),
                TextDelta(text="partial Provider output"),
                ResponseFailed(failure),
            ),
        )
    )
    clock = _clock()
    ids = DeterministicIdGenerator()
    store = SessionStore(root, clock=clock, id_generator=ids)
    application = AgentTurnApplication(
        provider=provider,
        workspace=Workspace(root),
        tool_registry=ToolRegistry(),
        clock=clock,
        id_generator=ids,
        session_store=store,
    )
    rendered: list[str] = []
    try:
        await application.run(
            "record the interrupted Provider response",
            on_event=lambda event: rendered.append(
                event.text if isinstance(event, TextDelta) else event.kind.value
            ),
        )
        raised = None
    except BaseException as exc:  # the failed Turn is the expected observation
        raised = type(exc).__name__
    records = _event_records(store, "session-0001")
    types = _event_types(records)
    checks = {
        "provider_request_failed": types.count(SessionEventType.MODEL_REQUEST_FAILED) == 1,
        "turn_failed": types.count(SessionEventType.TURN_FAILED) == 1,
        "no_assistant_message": SessionEventType.ASSISTANT_MESSAGE not in types,
        "partial_output_not_complete": "partial Provider output" in rendered
        and SessionEventType.MODEL_REQUEST_COMPLETED not in types,
        "terminal_event_last": types[-1] == SessionEventType.TURN_FAILED,
    }
    return {
        "case": "provider-interruption",
        "session_id": "session-0001",
        "raised": raised,
        "output_complete": False,
        "rendered_observation": rendered,
        "events": records,
        "checks": checks,
        "passed": all(checks.values()),
    }


async def _stream_cancellation(root: Path) -> dict[str, Any]:
    root.mkdir(parents=True, exist_ok=True)
    reached_stream = asyncio.Event()
    release_stream = asyncio.Event()

    class WaitingProvider:
        def stream(self, messages: object):
            del messages

            async def emit():
                yield ResponseStarted(request_id="response-cancelled")
                yield TextDelta(text="partial cancellation output")
                reached_stream.set()
                await release_stream.wait()

            return emit()

    clock = _clock()
    ids = DeterministicIdGenerator()
    store = SessionStore(root, clock=clock, id_generator=ids)
    application = AgentTurnApplication(
        provider=WaitingProvider(),
        workspace=Workspace(root),
        tool_registry=ToolRegistry(),
        clock=clock,
        id_generator=ids,
        session_store=store,
    )
    rendered: list[str] = []
    task = asyncio.create_task(
        application.run(
            "cancel while the Provider is streaming",
            on_event=lambda event: rendered.append(
                event.text if isinstance(event, TextDelta) else event.kind.value
            ),
        )
    )
    await asyncio.wait_for(reached_stream.wait(), timeout=2)
    task.cancel()
    try:
        await task
        raised = None
    except BaseException as exc:  # cancellation must remain visible and incomplete
        raised = type(exc).__name__
    release_stream.set()
    records = _event_records(store, "session-0001")
    types = _event_types(records)
    failed = next(
        record for record in records if record["event_type"] == SessionEventType.TURN_FAILED
    )
    checks = {
        "provider_request_failed": types.count(SessionEventType.MODEL_REQUEST_FAILED) == 1,
        "turn_failed": types.count(SessionEventType.TURN_FAILED) == 1,
        "cancellation_category": failed["payload"].get("category") == "cancellation",
        "partial_output_not_complete": "partial cancellation output" in rendered
        and SessionEventType.MODEL_REQUEST_COMPLETED not in types,
        "terminal_event_last": types[-1] == SessionEventType.TURN_FAILED,
    }
    return {
        "case": "stream-cancellation",
        "session_id": "session-0001",
        "raised": raised,
        "output_complete": False,
        "rendered_observation": rendered,
        "events": records,
        "checks": checks,
        "passed": all(checks.values()),
    }


def _create_started_session(root: Path) -> tuple[SessionStore, str]:
    (root / "note.txt").write_text("resume evidence\n", encoding="utf-8")
    clock = _clock()
    ids = DeterministicIdGenerator()
    store = SessionStore(root, clock=clock, id_generator=ids)
    writer = store.create("session-0001", created_at=clock.now())
    turn = writer.append(SessionEventType.TURN_STARTED, {}, turn_id="turn-original")
    user = writer.append(
        SessionEventType.USER_MESSAGE,
        {"role": "user", "content": "recover the interrupted Tool"},
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
                    "tool_call_id": "call-interrupted",
                    "name": "shell",
                    "arguments": {"command": "python -c \"print(1)\"", "working_directory": "."},
                }
            ],
        },
        turn_id=turn.turn_id,
        causation_id=completed.event_id,
    )
    proposed = writer.append(
        SessionEventType.TOOL_PROPOSED,
        {
            "tool_call_id": "call-interrupted",
            "name": "shell",
            "arguments": {"command": "python -c \"print(1)\"", "working_directory": "."},
        },
        turn_id=turn.turn_id,
        causation_id=assistant.event_id,
    )
    validated = writer.append(
        SessionEventType.TOOL_VALIDATED,
        {
            "tool_call_id": "call-interrupted",
            "name": "shell",
            "arguments": {"command": "python -c \"print(1)\"", "working_directory": "."},
            "risk": {
                "side_effect": "execute",
                "resources": ["."],
                "hazards": [],
                "summary": "run one controlled Shell command for recovery evidence",
            },
        },
        turn_id=turn.turn_id,
        causation_id=proposed.event_id,
    )
    writer.append(
        SessionEventType.TOOL_STARTED,
        {
            "tool_call_id": "call-interrupted",
            "name": "shell",
            "recovery": {
                "arguments": {
                    "command": "python -c \"print(1)\"",
                    "working_directory": ".",
                }
            },
        },
        turn_id=turn.turn_id,
        causation_id=validated.event_id,
    )
    writer.close()
    recovery_workspace = Workspace(root).for_session("session-0001")
    recovery_workspace.begin_tool_recovery(
        "call-interrupted",
        "shell",
        {
            "arguments": {
                "command": "python -c \"print(1)\"",
                "working_directory": ".",
            }
        },
    )
    recovery_workspace.update_tool_recovery(
        state="running",
        captured_preview={"content": "resume evidence"},
        process_evidence={"state": "running", "pid": 2147483647},
    )
    return store, "session-0001"


async def _resume_recovery(root: Path) -> dict[str, Any]:
    root.mkdir(parents=True, exist_ok=True)
    store, session_id = _create_started_session(root)
    inspection = store.inspect_resume(session_id)
    ids = DeterministicIdGenerator()
    clock = _clock()
    class AllowAll:
        def decide(self, request: object) -> PermissionDecision:
            del request
            return PermissionDecision.ALLOW

    application = AgentTurnApplication(
        provider=ScriptedFakeModelProvider(),
        workspace=Workspace(root),
        tool_registry=ToolRegistry([ShellTool()]),
        clock=clock,
        id_generator=ids,
        session_store=store,
        permission_gate=AllowAll(),
    )
    retry = await application.retry_interrupted(session_id)
    snapshot = store.read(session_id)
    records = [event.to_record() for event in snapshot.events]
    types = _event_types(records)
    terminal_ids = {
        record["payload"].get("tool_call_id")
        for record in records
        if record["event_type"]
        in {
            SessionEventType.TOOL_COMPLETED,
            SessionEventType.TOOL_FAILED,
            SessionEventType.TOOL_INTERRUPTED,
        }
    }
    checks = {
        "inspection_found_started_tool": len(inspection.interrupted_tools) == 1
        and inspection.interrupted_tools[0].tool_call_id == "call-interrupted",
        "inspection_has_process_evidence": inspection.interrupted_tools[0].evidence[
            "process_evidence"
        ]["alive"] is False,
        "old_call_closed_as_interrupted": types.count(SessionEventType.TOOL_INTERRUPTED) == 1
        and "call-interrupted" in terminal_ids,
        "retry_is_new_call": retry.new_tool_call_ids[0] != "call-interrupted",
        "new_call_has_terminal_result": retry.new_tool_call_ids[0] in terminal_ids,
        "recovery_events_are_durable": SessionEventType.RESUME_RECOVERY_RETRIED in types
        and types.count(SessionEventType.TURN_FAILED) == 2,
        "workspace_state_preserved": (root / "note.txt").read_text(encoding="utf-8")
        == "resume evidence\n",
        "no_original_replay": types.count(SessionEventType.TOOL_STARTED) == 2,
    }
    return {
        "case": "started-tool-resume",
        "session_id": session_id,
        "inspection": inspection.as_dict(),
        "retry": {
            "old_tool_call_ids": list(retry.old_tool_call_ids),
            "new_tool_call_ids": list(retry.new_tool_call_ids),
            "outcomes": [result.outcome.value for result in retry.tool_results],
        },
        "recovered_projection": {
            "status": snapshot.projection.status.value if snapshot.projection else None,
            "resumable": snapshot.projection.resumable if snapshot.projection else False,
            "current_turn": snapshot.projection.current_turn is not None
            if snapshot.projection
            else None,
        },
        "events": records,
        "checks": checks,
        "passed": all(checks.values()),
    }


async def run_trial(root: Path) -> dict[str, Any]:
    root.mkdir(parents=True, exist_ok=True)
    cases = {
        "provider_interruption": await _provider_interruption(root / "provider"),
        "stream_cancellation": await _stream_cancellation(root / "cancellation"),
        "resume_recovery": await _resume_recovery(root / "resume"),
    }
    return {
        "trial": root.name,
        "harness": "rl02-v1",
        "provider_condition": "deterministic controlled provider; no network",
        "cases": cases,
        "passed": all(case["passed"] for case in cases.values()),
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = asyncio.run(run_trial(args.output.resolve()))
    result_path = args.output / "result.json"
    result_path.write_text(json.dumps(result, indent=2, default=str) + "\n", encoding="utf-8")
    print(json.dumps({"trial": result["trial"], "passed": result["passed"]}, sort_keys=True))
    raise SystemExit(0 if result["passed"] else 1)


if __name__ == "__main__":
    main()
