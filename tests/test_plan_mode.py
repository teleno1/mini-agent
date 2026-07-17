from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from mini_agent.adapters.clocks import DeterministicClock
from mini_agent.adapters.ids import DeterministicIdGenerator
from mini_agent.adapters.session_store import SessionStore
from mini_agent.application.agent import AgentTurnApplication
from mini_agent.configuration import (
    ConfigurationResolver,
    ForbiddenConfigurationKey,
    SessionConfigurationService,
)
from mini_agent.domain.sessions import SessionEventType
from mini_agent.domain.streams import (
    ResponseCompleted,
    ResponseStarted,
    TextDelta,
    ToolCallCompleted,
    ToolCallStarted,
)
from mini_agent.providers.fake import ScriptedFakeModelProvider
from mini_agent.tools.contracts import ToolRegistry
from mini_agent.tools.files import ReadFileTool
from mini_agent.tools.workspace import Workspace


def _resolver(
    tmp_path: Path, *, environment: dict[str, str] | None = None
) -> ConfigurationResolver:
    return ConfigurationResolver(
        tmp_path,
        user_config_path=tmp_path / "missing-user.toml",
        project_config_path=tmp_path / ".mini-agent" / "config.toml",
        environment=environment or {},
    )


def _application(tmp_path: Path, *, plan_mode: bool) -> tuple[AgentTurnApplication, SessionStore]:
    (tmp_path / "one.txt").write_text("one\n", encoding="utf-8")
    (tmp_path / "two.txt").write_text("two\n", encoding="utf-8")
    provider = ScriptedFakeModelProvider(
        responses=(
            (
                ResponseStarted(request_id="request-tools"),
                ToolCallStarted(tool_call_id="read-one", name="read_file"),
                ToolCallCompleted(tool_call_id="read-one", arguments={"path": "one.txt"}),
                ToolCallStarted(tool_call_id="read-two", name="read_file"),
                ToolCallCompleted(tool_call_id="read-two", arguments={"path": "two.txt"}),
                ResponseCompleted(stop_reason="tool_calls"),
            ),
            (
                ResponseStarted(request_id="request-final"),
                TextDelta(text="Inspected both files."),
                ResponseCompleted(),
            ),
        )
    )
    clock = DeterministicClock(datetime(2026, 1, 1, tzinfo=UTC))
    ids = DeterministicIdGenerator()
    store = SessionStore(tmp_path, clock=clock, id_generator=ids)
    application = AgentTurnApplication(
        provider=provider,
        workspace=Workspace(tmp_path),
        tool_registry=ToolRegistry([ReadFileTool()]),
        clock=clock,
        id_generator=ids,
        session_store=store,
        configuration=_resolver(tmp_path).resolve(session_overrides={"plan_mode": plan_mode}),
    )
    return application, store


@pytest.mark.asyncio
async def test_complex_turn_is_unchanged_but_plan_is_disabled_by_default(tmp_path: Path) -> None:
    application, store = _application(tmp_path, plan_mode=False)

    result = await application.run("Inspect both files")
    events = store.read(result.session_id).events

    assert result.tool_call_count == 2
    assert result.assistant_message.content == "Inspected both files."
    assert SessionEventType.PLAN_UPDATED not in [event.event_type for event in events]
    assert SessionEventType.PLAN_RESET not in [event.event_type for event in events]


@pytest.mark.asyncio
async def test_explicit_plan_mode_preserves_existing_plan_lifecycle(tmp_path: Path) -> None:
    application, store = _application(tmp_path, plan_mode=True)

    result = await application.run("Inspect both files")
    events = store.read(result.session_id).events

    assert [event.event_type for event in events].count(SessionEventType.PLAN_UPDATED) >= 3
    assert store.read(result.session_id).projection is not None
    assert store.read(result.session_id).projection.turns[0].plan_snapshots


def test_plan_mode_is_runtime_only_and_session_persistence_uses_existing_event(
    tmp_path: Path,
) -> None:
    assert (
        _resolver(tmp_path, environment={"MINI_AGENT_PLAN_MODE": "true"})
        .resolve()
        .plan_mode
        is False
    )

    config_path = tmp_path / ".mini-agent" / "config.toml"
    config_path.parent.mkdir()
    config_path.write_text("plan_mode = true\n", encoding="utf-8")
    with pytest.raises(ForbiddenConfigurationKey):
        _resolver(tmp_path).resolve()
    config_path.unlink()

    store = SessionStore(tmp_path, id_generator=DeterministicIdGenerator())
    with store.create("session-1"):
        pass
    service = SessionConfigurationService(_resolver(tmp_path), store)

    enabled = service.update("session-1", {"plan_mode": True})
    assert enabled.plan_mode is True
    assert store.resume("session-1").configuration_overrides["plan_mode"] is True
    service.update("session-1", {"plan_mode": False})
    resumed = store.resume("session-1")
    assert resumed.configuration_overrides["plan_mode"] is False
    assert [event.event_type for event in resumed.events].count(
        SessionEventType.CONFIGURATION_CHANGED
    ) == 2
