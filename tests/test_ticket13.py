from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from mini_agent.adapters.clocks import DeterministicClock
from mini_agent.adapters.ids import DeterministicIdGenerator
from mini_agent.adapters.session_store import (
    ResumeChoice,
    SessionLockError,
    SessionStore,
)
from mini_agent.application.agent import AgentTurnApplication
from mini_agent.domain.plans import PlanSnapshot, PlanStep
from mini_agent.domain.sessions import SessionEventType
from mini_agent.providers.fake import ScriptedFakeModelProvider
from mini_agent.tools.contracts import PermissionDecision, PermissionRequest, ToolRegistry
from mini_agent.tools.files import ReadFileTool
from mini_agent.tools.workspace import Workspace


def _crashed_session(
    tmp_path: Path,
    *,
    name: str = "read_file",
    arguments: dict[str, object] | None = None,
    previous_instruction_hash: str | None = None,
) -> tuple[SessionStore, str]:
    (tmp_path / "AGENTS.md").write_text("rule: current\n", encoding="utf-8")
    (tmp_path / "note.txt").write_text("old\n", encoding="utf-8")
    clock = DeterministicClock(datetime(2026, 1, 1, tzinfo=UTC))
    store = SessionStore(tmp_path, clock=clock, id_generator=DeterministicIdGenerator())
    writer = store.create("crashed", created_at=clock.now())
    turn = writer.append(SessionEventType.TURN_STARTED, {}, turn_id="turn-1")
    user = writer.append(
        SessionEventType.USER_MESSAGE,
        {"role": "user", "content": "recover this work"},
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
    call_arguments = arguments or {"path": "note.txt"}
    assistant = writer.append(
        SessionEventType.ASSISTANT_MESSAGE,
        {
            "content": "",
            "tool_calls": [
                {"tool_call_id": "call-crashed", "name": name, "arguments": call_arguments}
            ],
        },
        turn_id=turn.turn_id,
        causation_id=completed.event_id,
    )
    writer.append(
        SessionEventType.TOOL_PROPOSED,
        {"tool_call_id": "call-crashed", "name": name, "arguments": call_arguments},
        turn_id=turn.turn_id,
        causation_id=assistant.event_id,
    )
    writer.append(
        SessionEventType.TOOL_VALIDATED,
        {
            "tool_call_id": "call-crashed",
            "name": name,
            "arguments": call_arguments,
            "risk": {
                "side_effect": "read" if name in {"read_file", "search_files"} else "write",
                "resources": ["note.txt"],
                "hazards": [],
                "summary": "test recovery call",
            },
        },
        turn_id=turn.turn_id,
    )
    writer.append(
        SessionEventType.TOOL_STARTED,
        {
            "tool_call_id": "call-crashed",
            "name": name,
            "recovery": {"arguments": call_arguments},
        },
        turn_id=turn.turn_id,
    )
    if previous_instruction_hash is not None:
        writer.append(
            SessionEventType.CONTEXT_MANIFEST_RECORDED,
            {
                "manifest": {
                    "instruction_hashes": [
                        {"path": "AGENTS.md", "sha256": previous_instruction_hash}
                    ]
                }
            },
            turn_id=turn.turn_id,
        )
    writer.close()
    return store, "crashed"


def _write_checkpoint(tmp_path: Path, session_id: str, old: bytes) -> None:
    checkpoint = tmp_path / ".mini-agent" / "sessions" / session_id / "checkpoints" / "checkpoint-1"
    (checkpoint / "before").mkdir(parents=True)
    (checkpoint / "before" / "0000.bin").write_bytes(old)
    (checkpoint / "manifest.json").write_text(
        json.dumps(
            {
                "checkpoint_id": "checkpoint-1",
                "files": [
                    {
                        "path": "note.txt",
                        "existed": True,
                        "sha256": hashlib.sha256(old).hexdigest(),
                        "bytes": len(old),
                    }
                ],
            }
        ),
        encoding="utf-8",
    )


def test_resume_inspects_reads_and_exit_does_not_rewrite_history(tmp_path: Path) -> None:
    store, session_id = _crashed_session(tmp_path)
    before = store.read(session_id).events

    inspection = store.inspect_resume(session_id)
    assert inspection.requires_recovery is True
    assert inspection.interrupted_tools[0].kind == "read"
    assert inspection.interrupted_tools[0].evidence["recommendation"]

    outcome = store.reconcile_resume(session_id, ResumeChoice.EXIT)
    assert outcome.resumed is None
    assert store.read(session_id).events == before


def test_resume_inspects_partial_patch_and_abandon_creates_one_uncertain_result(
    tmp_path: Path,
) -> None:
    old = b"old\n"
    store, session_id = _crashed_session(
        tmp_path,
        name="apply_patch",
        arguments={
            "operations": [
                {"operation": "update", "path": "note.txt", "old_text": "old", "new_text": "new"}
            ]
        },
    )
    _write_checkpoint(tmp_path, session_id, old)
    (tmp_path / "note.txt").write_text("partial\n", encoding="utf-8")

    inspection = store.inspect_resume(session_id)
    evidence = inspection.interrupted_tools[0].evidence
    assert evidence["state"] == "partial-or-raced"
    assert evidence["note"]

    outcome = store.reconcile_resume(session_id, ResumeChoice.ABANDON)
    assert outcome.resumed is not None
    events = store.read(session_id).events
    assert [event.event_type for event in events].count(SessionEventType.TOOL_INTERRUPTED) == 1
    assert SessionEventType.TOOL_COMPLETED not in [event.event_type for event in events]
    assert events[-1].event_type == SessionEventType.TURN_FAILED
    terminal = next(
        event for event in events if event.event_type == SessionEventType.TOOL_INTERRUPTED
    )
    assert terminal.payload["outcome"] == "interrupted"
    assert terminal.payload["result"]["data"]["confirmed_effect"] is False


def test_resume_distinguishes_completed_but_unrecorded_patch_and_reset_plan(
    tmp_path: Path,
) -> None:
    store, session_id = _crashed_session(
        tmp_path,
        name="apply_patch",
        arguments={
            "operations": [
                {"operation": "update", "path": "note.txt", "old_text": "old", "new_text": "new"}
            ]
        },
    )
    old = (tmp_path / "note.txt").read_bytes()
    writer = store.open_writer(session_id)
    writer.append(
        SessionEventType.PLAN_UPDATED,
        {
            "plan": PlanSnapshot(
                "plan-1",
                "recover",
                (PlanStep("edit", "Edit the file"),),
                datetime(2026, 1, 1, tzinfo=UTC),
            ).as_dict()
        },
        turn_id="turn-1",
    )
    writer.close()
    _write_checkpoint(tmp_path, session_id, old)
    (tmp_path / "note.txt").write_text("new\n", encoding="utf-8")

    inspection = store.inspect_resume(session_id)
    assert inspection.interrupted_tools[0].evidence["state"] == (
        "all-expected-bytes-present-but-not-proven"
    )
    store.reconcile_resume(session_id, ResumeChoice.RETRY)
    snapshot = store.read(session_id)
    assert snapshot.projection is not None
    assert snapshot.projection.current_turn is None
    assert snapshot.projection.current_plan is None
    assert SessionEventType.RESUME_RECOVERY_RETRIED in [
        event.event_type for event in snapshot.events
    ]


def test_resume_shell_includes_command_preview_and_process_evidence(tmp_path: Path) -> None:
    store, session_id = _crashed_session(
        tmp_path,
        name="shell",
        arguments={"command": "python -c 'print(1)'", "working_directory": "."},
    )
    workspace = Workspace(tmp_path).for_session(session_id)
    workspace.begin_tool_recovery("call-crashed", "shell", {"command": "python -c 'print(1)'"})
    workspace.update_tool_recovery(
        state="running",
        captured_preview={"stdout": "1", "stderr": ""},
        process_evidence={"state": "running", "pid": 2_147_483_647},
    )

    inspection = store.inspect_resume(session_id)
    evidence = inspection.interrupted_tools[0].evidence
    assert evidence["command"] == "python -c 'print(1)'"
    assert evidence["captured_preview"] == {"stdout": "1", "stderr": ""}
    assert evidence["process_evidence"]["alive"] is False


def test_resume_re_reads_instructions_and_persists_change_notice(tmp_path: Path) -> None:
    old_hash = hashlib.sha256(b"rule: old\n").hexdigest()
    (tmp_path / "AGENTS.md").write_text("rule: old\n", encoding="utf-8")
    store, session_id = _crashed_session(
        tmp_path,
        previous_instruction_hash=old_hash,
    )
    (tmp_path / "AGENTS.md").write_text("rule: current\n", encoding="utf-8")

    inspection = store.inspect_resume(session_id)
    assert inspection.instruction_change is True
    store.reconcile_resume(session_id, ResumeChoice.ABANDON)
    events = store.read(session_id).events
    assert SessionEventType.INSTRUCTION_CHANGED in [event.event_type for event in events]


def test_resume_blocks_active_writer_and_unknown_recovery_sidecar(tmp_path: Path) -> None:
    store, session_id = _crashed_session(tmp_path)
    writer = store.open_writer(session_id)
    try:
        with pytest.raises(SessionLockError):
            store.inspect_resume(session_id)
    finally:
        writer.close()

    recovery = tmp_path / ".mini-agent" / "sessions" / session_id / "recovery"
    recovery.mkdir(parents=True)
    (recovery / "unknown.json").write_text("{not-json", encoding="utf-8")
    inspection = store.inspect_resume(session_id)
    assert inspection.blocked_reason is not None
    assert "recovery evidence" in inspection.blocked_reason


@pytest.mark.asyncio
async def test_retry_interrupted_uses_new_call_id_and_permission_gate(tmp_path: Path) -> None:
    store, session_id = _crashed_session(tmp_path)
    clock = DeterministicClock(datetime(2026, 1, 1, tzinfo=UTC))
    ids = DeterministicIdGenerator()
    decisions: list[str] = []

    def allow(request: PermissionRequest) -> PermissionDecision:
        decisions.append(request.call.tool_call_id)
        return PermissionDecision.ALLOW

    class Gate:
        def decide(self, request: PermissionRequest) -> PermissionDecision:
            return allow(request)

    application = AgentTurnApplication(
        provider=ScriptedFakeModelProvider(),
        workspace=Workspace(tmp_path),
        tool_registry=ToolRegistry([ReadFileTool()]),
        clock=clock,
        id_generator=ids,
        session_store=store,
        permission_gate=Gate(),
    )

    result = await application.retry_interrupted(session_id)

    assert result.old_tool_call_ids == ("call-crashed",)
    assert result.new_tool_call_ids[0] != "call-crashed"
    assert decisions == list(result.new_tool_call_ids)
    assert result.tool_results[0].success is True
    events = store.read(session_id).events
    assert [event.event_type for event in events].count(SessionEventType.TOOL_INTERRUPTED) == 1
    assert [event.event_type for event in events].count(SessionEventType.TOOL_COMPLETED) == 1
    assert result.new_tool_call_ids[0] in [
        event.payload.get("tool_call_id")
        for event in events
        if event.event_type == SessionEventType.TOOL_PROPOSED
    ]
