import hashlib
import json
import stat
from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic import BaseModel

from mini_agent.adapters.clocks import DeterministicClock
from mini_agent.adapters.ids import DeterministicIdGenerator
from mini_agent.adapters.session_store import SessionStore
from mini_agent.application.agent import AgentTurnApplication
from mini_agent.configuration import EffectiveConfiguration, PermissionMode
from mini_agent.domain.sessions import SessionEventType
from mini_agent.domain.streams import (
    ResponseCompleted,
    ResponseStarted,
    TextDelta,
    ToolCallCompleted,
    ToolCallStarted,
)
from mini_agent.providers.fake import ScriptedFakeModelProvider
from mini_agent.tools.artifacts import (
    MAX_ARTIFACT_REREAD_BYTES,
    ArtifactReadInput,
    ArtifactReadTool,
)
from mini_agent.tools.contracts import (
    RiskAssessment,
    SideEffectCategory,
    ToolCall,
    ToolLimits,
    ToolOutcome,
    ToolRegistry,
    ToolResult,
)
from mini_agent.tools.workspace import Workspace


class _PayloadInput(BaseModel):
    marker: str = "payload"


class _PayloadTool:
    name = "payload"
    description = "Return a deterministic text payload for Artifact integration tests."
    side_effect = SideEffectCategory.READ
    input_model = _PayloadInput
    limits = ToolLimits(max_output_bytes=64 * 1024)

    def __init__(self, payload: str) -> None:
        self.payload = payload

    def assess(self, arguments: _PayloadInput) -> RiskAssessment:
        del arguments
        return RiskAssessment(
            side_effect=self.side_effect,
            resources=("payload",),
            summary="return a test payload",
        )

    async def execute(self, workspace: Workspace, arguments: _PayloadInput) -> ToolResult:
        del workspace, arguments
        return ToolResult.succeeded(
            ToolCall(tool_call_id="internal", name=self.name, arguments={}),
            {"output": self.payload},
        )


def _configuration() -> EffectiveConfiguration:
    return EffectiveConfiguration(
        model="fake",
        permission_mode=PermissionMode.SUGGEST,
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


def _provider() -> ScriptedFakeModelProvider:
    return ScriptedFakeModelProvider(
        responses=(
            (
                ResponseStarted(request_id="request-payload"),
                ToolCallStarted(tool_call_id="call-payload", name="payload"),
                ToolCallCompleted(tool_call_id="call-payload", arguments={}),
                ResponseCompleted(stop_reason="tool_calls"),
            ),
            (
                ResponseStarted(request_id="request-final"),
                TextDelta(text="Artifact inspected."),
                ResponseCompleted(),
            ),
        )
    )


def _application(tmp_path: Path, payload: str) -> tuple[AgentTurnApplication, SessionStore]:
    tmp_path.mkdir(parents=True, exist_ok=True)
    clock = DeterministicClock(datetime(2026, 1, 1, tzinfo=UTC))
    ids = DeterministicIdGenerator()
    store = SessionStore(tmp_path, clock=clock, id_generator=ids)
    application = AgentTurnApplication(
        provider=_provider(),
        workspace=Workspace(tmp_path),
        tool_registry=ToolRegistry([_PayloadTool(payload)]),
        clock=clock,
        id_generator=ids,
        session_store=store,
        configuration=_configuration(),
    )
    return application, store


def _application_with_provider(
    tmp_path: Path,
    payload: str,
    provider: ScriptedFakeModelProvider,
    extra_tools: tuple[object, ...] = (),
) -> tuple[AgentTurnApplication, SessionStore]:
    tmp_path.mkdir(parents=True, exist_ok=True)
    clock = DeterministicClock(datetime(2026, 1, 1, tzinfo=UTC))
    ids = DeterministicIdGenerator()
    store = SessionStore(tmp_path, clock=clock, id_generator=ids)
    application = AgentTurnApplication(
        provider=provider,
        workspace=Workspace(tmp_path),
        tool_registry=ToolRegistry([_PayloadTool(payload), *extra_tools]),
        clock=clock,
        id_generator=ids,
        session_store=store,
        configuration=_configuration(),
    )
    return application, store


def _payload_for_exact_result_bytes(target: int) -> str:
    for size in range(max(0, target - 256), target + 256):
        candidate = "x" * size
        if (
            len(json.dumps({"output": candidate}, ensure_ascii=False, sort_keys=True).encode())
            == target
        ):
            return candidate
    raise AssertionError(f"could not construct a result of exactly {target} bytes")


@pytest.mark.asyncio
async def test_large_tool_result_is_redacted_artifact_with_integrity_checked_reference(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    secret = "sk-ticket10-never-persist-this-secret"
    monkeypatch.setenv("MINI_AGENT_API_KEY", secret)
    application, store = _application(
        tmp_path,
        secret + "\nghp_ticket10_github_secret\n" + "x" * 40_000,
    )

    result = await application.run("inspect the large result")
    tool_result = result.tool_results[0]
    reference = json.loads(tool_result.content)["artifact"]
    session_directory = tmp_path / ".mini-agent" / "sessions" / result.session_id
    artifact_path = session_directory / reference["path"]

    assert tool_result.outcome == ToolOutcome.SUCCESS.value
    assert set(reference) == {
        "artifact_id",
        "path",
        "media_type",
        "byte_count",
        "sha256",
        "preview",
        "truncated",
    }
    assert reference["path"].startswith("artifacts/")
    assert artifact_path.resolve().is_relative_to(session_directory.resolve())
    content = artifact_path.read_bytes()
    assert reference["byte_count"] == len(content)
    assert reference["sha256"] == hashlib.sha256(content).hexdigest()
    assert reference["truncated"] is True
    assert reference["preview"] == content[: len(reference["preview"].encode())].decode()
    assert secret not in tool_result.content
    assert secret not in content.decode()
    assert "ghp_ticket10_github_secret" not in content.decode()

    events = store.read(result.session_id).events
    artifact_index = next(
        index for index, event in enumerate(events) if event.event_type == "artifact.written"
    )
    terminal_index = next(
        index
        for index, event in enumerate(events)
        if event.event_type == SessionEventType.TOOL_COMPLETED
    )
    assert artifact_index < terminal_index
    terminal = events[terminal_index]
    assert terminal.payload["artifact"] == reference
    assert store.read(result.session_id).projection is not None
    assert len(store.read(result.session_id).projection.artifacts) == 1

    artifact_path.chmod(stat.S_IREAD | stat.S_IWRITE)
    artifact_path.write_bytes(b"tampered")
    tampered = await ArtifactReadTool(store).execute(
        Workspace(tmp_path).for_session(result.session_id),
        ArtifactReadInput(artifact_id=reference["artifact_id"], max_bytes=64),
    )
    assert tampered.outcome is ToolOutcome.FAILED
    assert tampered.error is not None
    assert tampered.error.code == "artifact-integrity-failed"


@pytest.mark.asyncio
async def test_artifact_threshold_is_strictly_greater_than_32_kib(tmp_path: Path) -> None:
    exact_payload = _payload_for_exact_result_bytes(32 * 1024)
    exact_application, exact_store = _application(tmp_path / "exact", exact_payload)
    exact_result = await exact_application.run("exact threshold")
    exact_events = exact_store.read(exact_result.session_id).events
    assert "artifact.written" not in [event.event_type for event in exact_events]

    over_payload = _payload_for_exact_result_bytes(32 * 1024 + 1)
    over_application, over_store = _application(tmp_path / "over", over_payload)
    over_result = await over_application.run("over threshold")
    over_events = over_store.read(over_result.session_id).events
    assert "artifact.written" in [event.event_type for event in over_events]


@pytest.mark.asyncio
async def test_tool_result_over_absolute_ceiling_is_failed_and_not_persisted(
    tmp_path: Path,
) -> None:
    application, store = _application(tmp_path, "x" * (64 * 1024))

    result = await application.run("return too much output")
    events = store.read(result.session_id).events

    assert result.tool_results[0].outcome == ToolOutcome.FAILED.value
    failure = next(event for event in events if event.event_type == SessionEventType.TOOL_FAILED)
    assert failure.payload["result"]["error"]["code"] == "output-limit"
    assert SessionEventType.ARTIFACT_WRITTEN not in [event.event_type for event in events]
    assert SessionEventType.TOOL_COMPLETED not in [event.event_type for event in events]


@pytest.mark.asyncio
async def test_artifact_reader_uses_identity_not_model_path_and_bounds_ranges(
    tmp_path: Path,
) -> None:
    application, store = _application(tmp_path, "x" * 65_000)
    result = await application.run("create an artifact")
    reference = json.loads(result.tool_results[0].content)["artifact"]
    reader = ArtifactReadTool(store)
    workspace = Workspace(tmp_path).for_session(result.session_id)

    reread = await reader.execute(
        workspace,
        ArtifactReadInput(
            artifact_id=reference["artifact_id"],
            start_byte=10,
            max_bytes=64,
        ),
    )
    assert reread.outcome is ToolOutcome.SUCCESS
    assert reread.data["start_byte"] == 10
    assert len(reread.data["content"].encode()) <= 64
    assert reread.data["truncated"] is True

    bounded = await reader.execute(
        workspace,
        ArtifactReadInput(
            artifact_id=reference["artifact_id"],
            start_byte=0,
            max_bytes=MAX_ARTIFACT_REREAD_BYTES,
        ),
    )
    assert len(bounded.text.encode()) <= MAX_ARTIFACT_REREAD_BYTES
    assert bounded.data["truncated"] is True

    invalid_identity = await reader.execute(
        workspace,
        ArtifactReadInput(artifact_id="../events.jsonl", start_byte=0, max_bytes=64),
    )
    assert invalid_identity.outcome is ToolOutcome.FAILED
    assert invalid_identity.error is not None
    assert invalid_identity.error.code == "artifact-not-found"

    with pytest.raises(ValueError):
        ArtifactReadInput(
            artifact_id=reference["artifact_id"],
            start_byte=0,
            max_bytes=MAX_ARTIFACT_REREAD_BYTES + 1,
        )


@pytest.mark.asyncio
async def test_agent_can_reread_an_artifact_while_its_session_writer_is_open(
    tmp_path: Path,
) -> None:
    provider = ScriptedFakeModelProvider(
        responses=(
            (
                ResponseStarted(request_id="request-payload"),
                ToolCallStarted(tool_call_id="call-payload", name="payload"),
                ToolCallCompleted(tool_call_id="call-payload", arguments={}),
                ResponseCompleted(stop_reason="tool_calls"),
            ),
            (
                ResponseStarted(request_id="request-reread"),
                ToolCallStarted(tool_call_id="call-reread", name="read_artifact"),
                ToolCallCompleted(
                    tool_call_id="call-reread",
                    arguments={"artifact_id": "artifact-0001", "start_byte": 12, "max_bytes": 12},
                ),
                ResponseCompleted(stop_reason="tool_calls"),
            ),
            (
                ResponseStarted(request_id="request-final"),
                TextDelta(text="Read the requested range."),
                ResponseCompleted(),
            ),
        )
    )
    application, _ = _application_with_provider(
        tmp_path,
        "0123456789" * 4_000,
        provider,
        (ArtifactReadTool(),),
    )

    result = await application.run("create and reread the large result")

    assert result.tool_results[1].outcome == ToolOutcome.SUCCESS.value
    reread = json.loads(result.tool_results[1].content)
    assert reread["content"] == "012345678901"


@pytest.mark.asyncio
async def test_failed_artifact_reference_leaves_an_orphan_without_success(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from mini_agent.adapters.session_store import SessionPersistenceError, SessionWriter

    original_append = SessionWriter.append

    def fail_artifact_reference(self, event_type, payload, **kwargs):
        if event_type == SessionEventType.ARTIFACT_WRITTEN:
            raise SessionPersistenceError("injected Artifact reference failure")
        return original_append(self, event_type, payload, **kwargs)

    monkeypatch.setattr(SessionWriter, "append", fail_artifact_reference)
    application, store = _application(tmp_path, "x" * 40_000)

    result = await application.run("store the large result")
    events = store.read(result.session_id).events

    assert result.tool_results[0].outcome == ToolOutcome.FAILED.value
    assert SessionEventType.ARTIFACT_WRITTEN not in [event.event_type for event in events]
    assert SessionEventType.TOOL_COMPLETED not in [event.event_type for event in events]
    assert [event.event_type for event in events].count(SessionEventType.TOOL_FAILED) == 1
    assert len(store.list_artifact_orphans(result.session_id)) == 1


def test_uncommitted_artifact_is_detectable_as_an_orphan(tmp_path: Path) -> None:
    store = SessionStore(tmp_path, id_generator=DeterministicIdGenerator())
    writer = store.create("session-orphan")
    writer.write_artifact(b"orphaned", media_type="text/plain")
    writer.close()
    assert len(store.list_artifact_orphans("session-orphan")) == 1
