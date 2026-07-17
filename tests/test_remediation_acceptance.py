from __future__ import annotations

import json
from pathlib import Path
from typing import cast

from remediation_support import run_fake_cli_journey

from mini_agent.adapters.session_store import SessionStore
from mini_agent.context import ContextLayerName, ContextMessage
from mini_agent.domain.messages import AssistantMessage, ToolResultMessage
from mini_agent.domain.sessions import CURRENT_SCHEMA_VERSION, JSONValue, SessionEventType
from mini_agent.domain.streams import (
    ResponseCompleted,
    ResponseStarted,
    TextDelta,
    ToolCallCompleted,
    ToolCallStarted,
    UsageReported,
)


def test_one_interactive_fake_journey_proves_all_remediations(tmp_path: Path) -> None:
    task = "Inspect the repository, create a report, verify it, and summarize the changes."
    journey = run_fake_cli_journey(
        tmp_path,
        task,
        cli_args=("--plan-mode",),
        interactive=True,
        input_text=f"{task}\n2\n/exit\n",
        responses=(
            (
                ResponseStarted(request_id="request-write"),
                ToolCallStarted(tool_call_id="call-write", name="create_file"),
                ToolCallCompleted(
                    tool_call_id="call-write",
                    arguments={"path": "report.txt", "content": "report"},
                ),
                UsageReported(input_tokens=10, output_tokens=2),
                ResponseCompleted(stop_reason="tool_calls"),
            ),
            (
                ResponseStarted(request_id="request-final"),
                TextDelta(text="The report is ready."),
                UsageReported(input_tokens=11, output_tokens=4),
                ResponseCompleted(),
            ),
        ),
    )

    assert journey.exit_code == 0
    assert (tmp_path / "report.txt").read_text(encoding="utf-8") == "report"
    assert f"+ You\n|   > {task}" in journey.output
    assert "| Agent" in journey.output
    assert "Plan (live)" in journey.output
    assert "[TOOL START] create_file" in journey.output
    assert "[TOOL RESULT] create_file (report.txt) - completed" in journey.output
    assert "Choose [1 allow once / 2 allow exact for Session / 3 deny / 4 cancel]" in journey.output
    assert "Choice: 2" in journey.output
    assert "|   > The report is ready." in journey.output
    assert "Status: context 11/" in journey.output
    assert "Commands: /help  /plan  /config  /sessions  /exit" in journey.output
    assert "Turn 1" not in journey.output
    assert "\x1b" not in journey.output

    event_types = [event.event_type for event in journey.events]
    assert all(event.schema_version == CURRENT_SCHEMA_VERSION for event in journey.events)
    assert SessionEventType.CONFIGURATION_CHANGED in event_types
    assert event_types.count(SessionEventType.PLAN_UPDATED) >= 2
    assert event_types[-1] == SessionEventType.TURN_COMPLETED

    validated = next(
        event for event in journey.events if event.event_type == SessionEventType.TOOL_VALIDATED
    )
    permission = validated.payload["permission"]
    assert isinstance(permission, dict)
    assert permission.get("decision") == "allow"
    assert permission.get("scope") == "session"
    assert permission.get("resource_summary") == ["report.txt"]
    argument_hash = permission.get("argument_hash")
    assert isinstance(argument_hash, str)
    assert len(argument_hash) == 64

    assert len(journey.context_frames) == 2
    continuation = journey.context_frames[1]
    history = [
        message for message in continuation.messages if message.layer is ContextLayerName.HISTORY
    ]
    assert [message.role for message in history] == ["assistant", "tool"]
    assistant = history[0].message
    result = history[1].message
    assert isinstance(assistant, AssistantMessage)
    assert isinstance(result, ToolResultMessage)
    assert [call.tool_call_id for call in assistant.tool_calls] == ["call-write"]
    assert result.tool_call_id == "call-write"
    assert result.content

    assistant_event = next(
        event for event in journey.events if event.event_type == SessionEventType.ASSISTANT_MESSAGE
    )
    tool_completed_event = next(
        event for event in journey.events if event.event_type == SessionEventType.TOOL_COMPLETED
    )
    sources = cast(list[dict[str, JSONValue]], continuation.manifest.as_dict()["message_sources"])
    assert [source["source_kind"] for source in sources] == [
        "session-event",
        "session-event",
    ]
    assert [source["event_id"] for source in sources] == [
        assistant_event.event_id,
        tool_completed_event.event_id,
    ]
    assert [source["sequence"] for source in sources] == [
        assistant_event.sequence,
        tool_completed_event.sequence,
    ]
    assert [source["event_type"] for source in sources] == [
        SessionEventType.ASSISTANT_MESSAGE,
        SessionEventType.TOOL_COMPLETED,
    ]
    assert [source["projection"] for source in sources] == [
        "assistant-message",
        "tool-result-message",
    ]
    assert all(
        set(source) == {"source_kind", "event_id", "sequence", "event_type", "projection"}
        for source in sources
    )
    manifest_text = json.dumps(journey.manifests[1], ensure_ascii=False)
    assert task not in manifest_text
    assert "content" not in manifest_text
    assert "api_key" not in manifest_text

    resumed = SessionStore(tmp_path).resume(journey.session_id)
    assert resumed.configuration_overrides["plan_mode"] is True
    assert resumed.projection is not None
    assert resumed.projection.current_plan is None
    assert resumed.projection.turns[0].plan_snapshots


def test_fake_journey_context_messages_keep_provider_roles_typed(tmp_path: Path) -> None:
    journey = run_fake_cli_journey(tmp_path, "Explain the project")

    frame = journey.context_frames[0]
    assert all(
        isinstance(message, ContextMessage)
        and message.role in {"system", "developer", "user", "assistant", "tool"}
        for message in frame.messages
    )
    assert all(
        message.message is None
        for message in frame.messages
        if message.layer is not ContextLayerName.HISTORY
    )
    assert journey.manifests[0]["message_sources"] == []
