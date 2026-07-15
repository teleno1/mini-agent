from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path

import pytest

import mini_agent.tools.patches as patch_module
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
    PermissionRequest,
    RiskAssessment,
    SideEffectCategory,
    ToolCall,
    ToolOutcome,
    ToolRegistry,
)
from mini_agent.tools.patches import (
    ApplyPatchInput,
    ApplyPatchTool,
    CreateFileInput,
    CreateFileTool,
)
from mini_agent.tools.workspace import Workspace


def _patch(*operations: dict[str, object]) -> ApplyPatchInput:
    return ApplyPatchInput.model_validate({"operations": list(operations)})


@pytest.mark.asyncio
async def test_multi_file_exact_patch_commits_and_creates_checkpoint(tmp_path: Path) -> None:
    (tmp_path / "update.txt").write_text("before\n", encoding="utf-8")
    (tmp_path / "delete.txt").write_text("remove me\n", encoding="utf-8")

    result = await ApplyPatchTool().execute(
        Workspace(tmp_path),
        _patch(
            {"op": "add", "path": "added.txt", "content": "new\n"},
            {"op": "update", "path": "update.txt", "old": "before", "new": "after"},
            {"op": "delete", "path": "delete.txt"},
        ),
    )

    assert result.outcome is ToolOutcome.SUCCESS
    assert (tmp_path / "added.txt").read_text(encoding="utf-8") == "new\n"
    assert (tmp_path / "update.txt").read_text(encoding="utf-8") == "after\n"
    assert not (tmp_path / "delete.txt").exists()
    assert result.data["changed_files"] == ["added.txt", "update.txt", "delete.txt"]
    checkpoint_id = result.data["checkpoint_id"]
    assert (tmp_path / ".mini-agent" / "checkpoints" / checkpoint_id / "manifest.json").exists()


@pytest.mark.asyncio
async def test_patch_validates_every_operation_before_writing(tmp_path: Path) -> None:
    target = tmp_path / "target.txt"
    target.write_text("one\n", encoding="utf-8")
    other = tmp_path / "other.txt"
    other.write_text("keep\n", encoding="utf-8")

    result = await ApplyPatchTool().execute(
        Workspace(tmp_path),
        _patch(
            {"op": "update", "path": "target.txt", "old": "one", "new": "changed"},
            {
                "op": "update",
                "path": "missing/other.txt",
                "old": "not-present",
                "new": "bad",
            },
        ),
    )

    assert result.outcome is ToolOutcome.FAILED
    assert result.error is not None
    assert result.error.code == "missing"
    assert target.read_text(encoding="utf-8") == "one\n"
    assert other.read_text(encoding="utf-8") == "keep\n"
    assert not (tmp_path / "missing").exists()


@pytest.mark.asyncio
async def test_partial_commit_rolls_back_known_failure(tmp_path: Path, monkeypatch) -> None:
    first = tmp_path / "first.txt"
    second = tmp_path / "second.txt"
    first.write_text("first\n", encoding="utf-8")
    second.write_text("second\n", encoding="utf-8")
    original_replace = patch_module.os.replace
    calls = 0

    def fail_on_first_target_replace(source, destination):
        nonlocal calls
        calls += 1
        # Two snapshots and the manifest are durable before the first target replace;
        # fail the second target so rollback has an applied file to restore.
        if calls == 5:
            raise OSError("simulated target failure")
        return original_replace(source, destination)

    monkeypatch.setattr("mini_agent.tools.patches.os.replace", fail_on_first_target_replace)
    result = await ApplyPatchTool().execute(
        Workspace(tmp_path),
        _patch(
            {"op": "update", "path": "first.txt", "old": "first", "new": "changed-first"},
            {"op": "update", "path": "second.txt", "old": "second", "new": "changed-second"},
        ),
    )

    assert result.outcome is ToolOutcome.FAILED
    assert result.data["rolled_back"] is True
    assert first.read_text(encoding="utf-8") == "first\n"
    assert second.read_text(encoding="utf-8") == "second\n"
    assert result.data["rollback_evidence"]


@pytest.mark.asyncio
async def test_create_file_collision_and_parent_policy_never_overwrite(tmp_path: Path) -> None:
    existing = tmp_path / "existing.txt"
    existing.write_text("original", encoding="utf-8")
    tool = CreateFileTool()

    collision = await tool.execute(
        Workspace(tmp_path), CreateFileInput(path="existing.txt", content="replacement")
    )
    assert collision.outcome is ToolOutcome.FAILED
    assert collision.error is not None and collision.error.code == "exists"
    assert existing.read_text(encoding="utf-8") == "original"

    created = await tool.execute(
        Workspace(tmp_path),
        CreateFileInput(path="nested/deep/new.txt", content="created", create_parents=True),
    )
    assert created.outcome is ToolOutcome.SUCCESS
    assert (tmp_path / "nested/deep/new.txt").read_text(encoding="utf-8") == "created"


def test_permission_modes_and_exact_session_grants() -> None:
    request = PermissionRequest(
        ToolCall(tool_call_id="call-1", name="apply_patch", arguments={"path": "a.txt"}),
        # A normal Add/Update has no delete/protected hazard.
        risk=RiskAssessment(
            side_effect=SideEffectCategory.WRITE,
            resources=("a.txt",),
            summary="update a file",
        ),
    )
    assert PermissionPolicyGate(PermissionMode.SUGGEST).decide(request).value == "deny"
    assert PermissionPolicyGate(PermissionMode.AUTO_EDIT).decide(request).value == "allow"

    choices = iter([ConfirmationChoice.ALLOW_FOR_SESSION, ConfirmationChoice.DENY])
    gate = PermissionPolicyGate(PermissionMode.SUGGEST, interaction=lambda _preview: next(choices))
    assert gate.decide(request).value == "allow"
    assert gate.decide(request).value == "allow"
    changed = PermissionRequest(
        ToolCall(tool_call_id="call-2", name="apply_patch", arguments={"path": "b.txt"}),
        risk=request.risk.model_copy(update={"resources": ("b.txt",)}),
    )
    assert gate.decide(changed).value == "deny"


def test_protected_writes_always_require_focused_confirmation(tmp_path: Path) -> None:
    workspace = Workspace(tmp_path)
    assert workspace.is_protected_path("AGENTS.md")
    request = PermissionRequest(
        ToolCall(tool_call_id="call-protected", name="apply_patch", arguments={}),
        risk=RiskAssessment(
            side_effect=SideEffectCategory.WRITE,
            resources=("AGENTS.md",),
            hazards=("protected-path",),
            summary="modify Protected Path instructions",
        ),
    )
    choices = iter([ConfirmationChoice.ALLOW_FOR_SESSION, ConfirmationChoice.DENY])
    gate = PermissionPolicyGate(
        PermissionMode.FULL_AUTO,
        interaction=lambda _preview: next(choices),
    )
    assert gate.decide(request).value == "allow"
    # Protected writes are deliberately not silently reused from a Session grant.
    assert gate.decide(request).value == "deny"


@pytest.mark.asyncio
async def test_cancelled_partial_commit_rolls_back_and_reports_cancelled(
    tmp_path: Path, monkeypatch
) -> None:
    first = tmp_path / "first.txt"
    second = tmp_path / "second.txt"
    first.write_text("first\n", encoding="utf-8")
    second.write_text("second\n", encoding="utf-8")
    committed = asyncio.Event()
    original_commit = patch_module._commit_change
    count = 0

    def observed_commit(workspace, change):
        nonlocal count
        original_commit(workspace, change)
        count += 1
        if count == 1:
            committed.set()

    monkeypatch.setattr("mini_agent.tools.patches._commit_change", observed_commit)
    task = asyncio.create_task(
        ApplyPatchTool().execute(
            Workspace(tmp_path),
            _patch(
                {"op": "update", "path": "first.txt", "old": "first", "new": "changed-first"},
                {"op": "update", "path": "second.txt", "old": "second", "new": "changed-second"},
            ),
        )
    )
    await committed.wait()
    task.cancel()
    result = await task

    assert result.outcome is ToolOutcome.CANCELLED
    assert result.data["rolled_back"] is True
    assert first.read_text(encoding="utf-8") == "first\n"
    assert second.read_text(encoding="utf-8") == "second\n"


@pytest.mark.asyncio
async def test_agent_auto_edit_persists_permission_and_terminal_events(tmp_path: Path) -> None:
    target = tmp_path / "note.txt"
    target.write_text("old\n", encoding="utf-8")
    provider = ScriptedFakeModelProvider(
        responses=(
            (
                ResponseStarted(request_id="request-patch"),
                ToolCallStarted(tool_call_id="call-patch", name="apply_patch"),
                ToolCallArgumentDelta(
                    tool_call_id="call-patch",
                    arguments='{"operations":[{"operation":"update","path":"note.txt",',
                ),
                ToolCallArgumentDelta(
                    tool_call_id="call-patch",
                    arguments='"old_text":"old","new_text":"new"}]}',
                ),
                ToolCallCompleted(tool_call_id="call-patch"),
                ResponseCompleted(stop_reason="tool_calls"),
            ),
            (
                ResponseStarted(request_id="request-final"),
                TextDelta(text="Updated."),
                ResponseCompleted(),
            ),
        )
    )
    clock = DeterministicClock(datetime(2026, 1, 1, tzinfo=UTC))
    ids = DeterministicIdGenerator()
    store = SessionStore(tmp_path, clock=clock, id_generator=ids)
    configuration = EffectiveConfiguration(
        model="fake",
        permission_mode=PermissionMode.AUTO_EDIT,
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
        tool_registry=ToolRegistry([ApplyPatchTool()]),
        clock=clock,
        id_generator=ids,
        session_store=store,
        configuration=configuration,
    )

    result = await application.run("update the note")

    assert result.assistant_message.content == "Updated."
    assert target.read_text(encoding="utf-8") == "new\n"
    events = store.read(result.session_id).events
    validated = next(
        event for event in events if event.event_type == SessionEventType.TOOL_VALIDATED
    )
    permission = validated.payload["permission"]
    assert permission["decision"] == "allow"
    assert permission["matched_rule"] == "auto-edit-write"
    assert "old_text" not in permission
    assert any(event.event_type == SessionEventType.TOOL_COMPLETED for event in events)
