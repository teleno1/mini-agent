"""Pure Session Event and projection models.

The event log is the authority for a Session.  Filesystem concerns live in the
adapter; this module only defines the typed facts that can be recorded and the
disposable view rebuilt from those facts.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from datetime import datetime
from enum import StrEnum
from typing import cast

from mini_agent.domain.messages import (
    AssistantMessage,
    Message,
    ToolCallBlock,
    ToolResultMessage,
    UserMessage,
)

CURRENT_SCHEMA_VERSION = 1
SUPPORTED_SCHEMA_VERSIONS = frozenset({0, CURRENT_SCHEMA_VERSION})

type JSONValue = None | bool | int | float | str | list["JSONValue"] | dict[str, "JSONValue"]


class SessionEventType(StrEnum):
    """Event names used by the text-only Session lifecycle."""

    SESSION_CREATED = "session.created"
    TURN_STARTED = "turn.started"
    USER_MESSAGE = "user.message"
    MODEL_REQUEST_STARTED = "model.request.started"
    MODEL_REQUEST_COMPLETED = "model.request.completed"
    MODEL_REQUEST_FAILED = "model.request.failed"
    ASSISTANT_MESSAGE = "assistant.message"
    TURN_COMPLETED = "turn.completed"
    TURN_FAILED = "turn.failed"
    CONFIGURATION_CHANGED = "configuration.changed"
    CONTEXT_MANIFEST_RECORDED = "context.manifest.recorded"
    INSTRUCTION_CHANGED = "instruction.changed"
    TOOL_PROPOSED = "tool.proposed"
    TOOL_VALIDATED = "tool.validated"
    TOOL_STARTED = "tool.started"
    TOOL_COMPLETED = "tool.completed"
    TOOL_FAILED = "tool.failed"
    TOOL_INTERRUPTED = "tool.interrupted"


class SessionStatus(StrEnum):
    """Rebuildable user-visible Session status."""

    IDLE = "idle"
    ACTIVE = "active"
    COMPLETED = "completed"
    FAILED = "failed"


class ToolCallStatus(StrEnum):
    """Durable lifecycle status for one proposed Tool Call."""

    PROPOSED = "proposed"
    VALIDATED = "validated"
    STARTED = "started"
    COMPLETED = "completed"
    FAILED = "failed"
    INTERRUPTED = "interrupted"


class InvalidSessionEvents(ValueError):
    """Raised when otherwise parseable events cannot form a valid projection."""


@dataclass(frozen=True, slots=True)
class SessionEvent:
    """One versioned, append-only fact in a Session."""

    schema_version: int
    event_id: str
    sequence: int
    session_id: str
    event_type: str
    timestamp: datetime
    turn_id: str | None
    causation_id: str | None
    payload: dict[str, JSONValue]

    def __post_init__(self) -> None:
        if self.schema_version < 0:
            raise ValueError("schema version cannot be negative")
        if not self.event_id.strip():
            raise ValueError("event ID cannot be blank")
        if self.sequence < 1:
            raise ValueError("event sequence must start at one")
        if not self.session_id.strip():
            raise ValueError("session ID cannot be blank")
        if not self.event_type.strip():
            raise ValueError("event type cannot be blank")
        if self.timestamp.tzinfo is None:
            raise ValueError("event timestamp must be timezone-aware")

    def to_record(self) -> dict[str, JSONValue]:
        """Return the stable JSON-compatible representation of this event."""

        return {
            "schema_version": self.schema_version,
            "event_id": self.event_id,
            "sequence": self.sequence,
            "session_id": self.session_id,
            "event_type": self.event_type,
            "timestamp": self.timestamp.isoformat(),
            "turn_id": self.turn_id,
            "causation_id": self.causation_id,
            "payload": self.payload,
        }

    @classmethod
    def from_record(cls, record: Mapping[str, object]) -> SessionEvent:
        """Parse a current-schema record after any adapter-level migration."""

        schema_version = _int_field(record, "schema_version")
        event_id = _string_field(record, "event_id")
        sequence = _int_field(record, "sequence")
        session_id = _string_field(record, "session_id")
        event_type = _string_field(record, "event_type")
        timestamp_value = record.get("timestamp")
        if not isinstance(timestamp_value, str):
            raise ValueError("event timestamp must be an ISO-8601 string")
        try:
            timestamp = datetime.fromisoformat(timestamp_value)
        except ValueError as exc:
            raise ValueError("event timestamp is not valid ISO-8601") from exc
        turn_id = _optional_string_field(record, "turn_id")
        causation_id = _optional_string_field(record, "causation_id")
        payload_value = record.get("payload")
        if not isinstance(payload_value, dict):
            raise ValueError("event payload must be an object")
        payload = cast(dict[str, JSONValue], payload_value)
        return cls(
            schema_version=schema_version,
            event_id=event_id,
            sequence=sequence,
            session_id=session_id,
            event_type=event_type,
            timestamp=timestamp,
            turn_id=turn_id,
            causation_id=causation_id,
            payload=payload,
        )


@dataclass(frozen=True, slots=True)
class ToolCallProjection:
    """Durable projection of one proposed Tool Call and its terminal result."""

    tool_call_id: str
    name: str
    arguments: dict[str, JSONValue]
    status: ToolCallStatus
    risk: dict[str, JSONValue] | None = None
    result: ToolResultMessage | None = None


@dataclass(frozen=True, slots=True)
class TurnProjection:
    """Current durable view of one Turn."""

    turn_id: str
    status: SessionStatus
    user_message: UserMessage | None
    assistant_message: AssistantMessage | None
    started_at: datetime
    completed_at: datetime | None
    usage_input_tokens: int
    usage_output_tokens: int
    request_started: bool = False
    request_completed: bool = False
    request_failed: bool = False
    request_count: int = 0
    assistant_messages: tuple[AssistantMessage, ...] = ()
    tool_calls: tuple[ToolCallProjection, ...] = ()


@dataclass(frozen=True, slots=True)
class SessionProjection:
    """Disposable current state rebuilt from the authoritative event history."""

    session_id: str
    status: SessionStatus
    created_at: datetime
    updated_at: datetime
    last_sequence: int
    turns: tuple[TurnProjection, ...]
    messages: tuple[Message, ...]
    configuration_overrides: Mapping[str, JSONValue] = field(default_factory=dict)
    context_manifests: tuple[dict[str, JSONValue], ...] = ()

    @property
    def current_turn(self) -> TurnProjection | None:
        """Return the unfinished Turn, if the history contains one."""

        for turn in reversed(self.turns):
            if turn.status is SessionStatus.ACTIVE:
                return turn
        return None

    @property
    def resumable(self) -> bool:
        """Whether a new text-only Turn can safely be started from this view."""

        return self.current_turn is None


def rebuild_projection(events: tuple[SessionEvent, ...]) -> SessionProjection:
    """Rebuild Session metadata and messages in event-sequence order."""

    if not events:
        raise InvalidSessionEvents("a Session must contain at least one event")

    session_id = events[0].session_id
    if events[0].event_type != SessionEventType.SESSION_CREATED:
        raise InvalidSessionEvents("the first event must be session.created")

    turns: dict[str, TurnProjection] = {}
    request_states: dict[str, dict[str, str]] = {}
    assistant_requests: dict[str, set[str]] = {}
    tool_states: dict[str, ToolCallProjection] = {}
    messages: list[Message] = []
    configuration_overrides: list[dict[str, JSONValue]] = []
    context_manifests: list[dict[str, JSONValue]] = []
    status = SessionStatus.IDLE
    created_at = events[0].timestamp

    for event in events:
        if event.session_id != session_id:
            raise InvalidSessionEvents("all events must belong to one Session")
        try:
            event_type = SessionEventType(event.event_type)
        except ValueError:
            raise InvalidSessionEvents(f"unknown current-schema event: {event.event_type}")

        if event_type is SessionEventType.SESSION_CREATED:
            if event is not events[0]:
                raise InvalidSessionEvents("session.created may occur only once")
            continue

        if event_type is SessionEventType.CONFIGURATION_CHANGED:
            reset = event.payload.get("reset", False)
            if not isinstance(reset, bool):
                raise InvalidSessionEvents("configuration.changed reset must be a boolean")
            if reset:
                configuration_overrides.clear()
            overrides = event.payload.get("overrides", {})
            if not isinstance(overrides, dict):
                raise InvalidSessionEvents("configuration.changed overrides must be an object")
            current_overrides = (
                {} if reset or not configuration_overrides else dict(configuration_overrides[-1])
            )
            current_overrides.update(cast(dict[str, JSONValue], dict(overrides)))
            configuration_overrides[:] = [current_overrides] if current_overrides else []
            continue

        if event_type is SessionEventType.CONTEXT_MANIFEST_RECORDED:
            manifest = event.payload.get("manifest", event.payload)
            if not isinstance(manifest, dict):
                raise InvalidSessionEvents("context.manifest.recorded must contain an object")
            context_manifests.append(cast(dict[str, JSONValue], dict(manifest)))
            continue

        if event_type is SessionEventType.INSTRUCTION_CHANGED:
            continue

        if event_type is SessionEventType.TURN_STARTED:
            turn_id = event.turn_id or _payload_string(event, "turn_id")
            if turn_id in turns:
                raise InvalidSessionEvents(f"Turn {turn_id!r} started more than once")
            turns[turn_id] = TurnProjection(
                turn_id=turn_id,
                status=SessionStatus.ACTIVE,
                user_message=None,
                assistant_message=None,
                started_at=event.timestamp,
                completed_at=None,
                usage_input_tokens=0,
                usage_output_tokens=0,
            )
            request_states[turn_id] = {}
            assistant_requests[turn_id] = set()
            status = SessionStatus.ACTIVE
            continue

        turn = _turn_for_event(turns, event)
        if event_type is SessionEventType.USER_MESSAGE:
            content = _payload_string(event, "content")
            user_message = UserMessage(content)
            if turn.user_message is not None:
                raise InvalidSessionEvents(f"Turn {turn.turn_id!r} has two user messages")
            turns[turn.turn_id] = replace(turn, user_message=user_message)
            messages.append(user_message)
        elif event_type is SessionEventType.MODEL_REQUEST_STARTED:
            if turn.user_message is None:
                raise InvalidSessionEvents(
                    f"Turn {turn.turn_id!r} started a model request before its user message"
                )
            request_id = _payload_string(event, "request_id")
            states = request_states[turn.turn_id]
            if request_id in states or "started" in states.values():
                raise InvalidSessionEvents(
                    f"Turn {turn.turn_id!r} started an overlapping model request"
                )
            states[request_id] = "started"
            turns[turn.turn_id] = replace(
                turn,
                request_started=True,
                request_count=turn.request_count + 1,
            )
        elif event_type is SessionEventType.MODEL_REQUEST_COMPLETED:
            completed_request_id = _request_id_for_event(
                event, request_states[turn.turn_id], "started"
            )
            if completed_request_id is None:
                raise InvalidSessionEvents(
                    f"Turn {turn.turn_id!r} has an out-of-order model request completion"
                )
            request_states[turn.turn_id][completed_request_id] = "completed"
            turns[turn.turn_id] = replace(
                turn,
                usage_input_tokens=turn.usage_input_tokens
                + _payload_nonnegative_int(event, "input_tokens"),
                usage_output_tokens=turn.usage_output_tokens
                + _payload_nonnegative_int(event, "output_tokens"),
                request_completed=True,
            )
        elif event_type is SessionEventType.MODEL_REQUEST_FAILED:
            failed_request_id = _request_id_for_event(
                event, request_states[turn.turn_id], "started"
            )
            if failed_request_id is None:
                raise InvalidSessionEvents(
                    f"Turn {turn.turn_id!r} has an out-of-order model request failure"
                )
            request_states[turn.turn_id][failed_request_id] = "failed"
            turns[turn.turn_id] = replace(turn, request_failed=True)
        elif event_type is SessionEventType.ASSISTANT_MESSAGE:
            assistant_request_id = _request_id_for_event(
                event,
                request_states[turn.turn_id],
                "completed",
                consumed=assistant_requests[turn.turn_id],
            )
            if assistant_request_id is None:
                raise InvalidSessionEvents(
                    f"Turn {turn.turn_id!r} has an assistant message before request completion"
                )
            assistant_requests[turn.turn_id].add(assistant_request_id)
            content_value = event.payload.get("content", "")
            if not isinstance(content_value, str):
                raise InvalidSessionEvents("assistant.message content must be a string")
            assistant_message = AssistantMessage(
                content_value,
                _payload_tool_calls(event),
            )
            turns[turn.turn_id] = replace(
                turn,
                assistant_message=assistant_message,
                assistant_messages=(*turn.assistant_messages, assistant_message),
            )
            messages.append(assistant_message)
        elif event_type is SessionEventType.TOOL_PROPOSED:
            call = ToolCallProjection(
                tool_call_id=_payload_string(event, "tool_call_id"),
                name=_payload_string(event, "name"),
                arguments=_payload_object(event, "arguments"),
                status=ToolCallStatus.PROPOSED,
            )
            if call.tool_call_id in tool_states:
                raise InvalidSessionEvents(f"Tool Call {call.tool_call_id!r} was proposed twice")
            tool_states[call.tool_call_id] = call
            turns[turn.turn_id] = replace(turn, tool_calls=(*turn.tool_calls, call))
        elif event_type is SessionEventType.TOOL_VALIDATED:
            call = _tool_for_event(tool_states, event)
            if call.status is not ToolCallStatus.PROPOSED:
                raise InvalidSessionEvents(
                    f"Tool Call {call.tool_call_id!r} validated out of order"
                )
            risk = event.payload.get("risk", {})
            if not isinstance(risk, dict):
                raise InvalidSessionEvents("tool.validated risk must be an object")
            updated = replace(call, status=ToolCallStatus.VALIDATED, risk=risk)
            tool_states[call.tool_call_id] = updated
            turns[turn.turn_id] = replace(turn, tool_calls=_replace_tool(turn.tool_calls, updated))
        elif event_type is SessionEventType.TOOL_STARTED:
            call = _tool_for_event(tool_states, event)
            if call.status is not ToolCallStatus.VALIDATED:
                raise InvalidSessionEvents(f"Tool Call {call.tool_call_id!r} started out of order")
            updated = replace(call, status=ToolCallStatus.STARTED)
            tool_states[call.tool_call_id] = updated
            turns[turn.turn_id] = replace(turn, tool_calls=_replace_tool(turn.tool_calls, updated))
        elif event_type in {
            SessionEventType.TOOL_COMPLETED,
            SessionEventType.TOOL_FAILED,
            SessionEventType.TOOL_INTERRUPTED,
        }:
            call = _tool_for_event(tool_states, event)
            outcome = _payload_string(event, "outcome")
            if call.status in {
                ToolCallStatus.COMPLETED,
                ToolCallStatus.FAILED,
                ToolCallStatus.INTERRUPTED,
            }:
                raise InvalidSessionEvents(f"Tool Call {call.tool_call_id!r} has two results")
            if (
                event_type is SessionEventType.TOOL_COMPLETED
                and call.status is not ToolCallStatus.STARTED
            ):
                raise InvalidSessionEvents(
                    f"Tool Call {call.tool_call_id!r} completed before it started"
                )
            result_text = event.payload.get("result_text", "")
            if not isinstance(result_text, str):
                raise InvalidSessionEvents("Tool terminal result_text must be a string")
            result = ToolResultMessage(call.tool_call_id, result_text, outcome)
            status_value = ToolCallStatus(event_type.value.rsplit(".", 1)[-1])
            updated = replace(call, status=status_value, result=result)
            tool_states[call.tool_call_id] = updated
            turns[turn.turn_id] = replace(turn, tool_calls=_replace_tool(turn.tool_calls, updated))
            messages.append(result)
        elif event_type is SessionEventType.TURN_COMPLETED:
            if (
                turn.user_message is None
                or turn.assistant_message is None
                or not turn.request_completed
                or turn.assistant_message.tool_calls
                or any(
                    call.status
                    not in {
                        ToolCallStatus.COMPLETED,
                        ToolCallStatus.FAILED,
                        ToolCallStatus.INTERRUPTED,
                    }
                    for call in turn.tool_calls
                )
                or any(state == "started" for state in request_states[turn.turn_id].values())
            ):
                raise InvalidSessionEvents(f"Turn {turn.turn_id!r} completed without messages")
            turns[turn.turn_id] = replace(
                turn,
                status=SessionStatus.COMPLETED,
                completed_at=event.timestamp,
            )
            status = SessionStatus.COMPLETED
        elif event_type is SessionEventType.TURN_FAILED:
            if any(state == "started" for state in request_states[turn.turn_id].values()) or any(
                call.status is ToolCallStatus.STARTED for call in turn.tool_calls
            ):
                raise InvalidSessionEvents(
                    f"Turn {turn.turn_id!r} failed before an operation reached a terminal state"
                )
            turns[turn.turn_id] = replace(
                turn,
                status=SessionStatus.FAILED,
                completed_at=event.timestamp,
            )
            status = SessionStatus.FAILED

    return SessionProjection(
        session_id=session_id,
        status=status,
        created_at=created_at,
        updated_at=events[-1].timestamp,
        last_sequence=events[-1].sequence,
        turns=tuple(turns.values()),
        messages=tuple(messages),
        configuration_overrides=(
            dict(configuration_overrides[-1]) if configuration_overrides else {}
        ),
        context_manifests=tuple(context_manifests),
    )


def _turn_for_event(turns: dict[str, TurnProjection], event: SessionEvent) -> TurnProjection:
    turn_id = event.turn_id or _payload_string(event, "turn_id")
    try:
        return turns[turn_id]
    except KeyError as exc:
        raise InvalidSessionEvents(f"event refers to unknown Turn {turn_id!r}") from exc


def _request_id_for_event(
    event: SessionEvent,
    states: dict[str, str],
    required_state: str,
    *,
    consumed: set[str] | None = None,
) -> str | None:
    value = event.payload.get("request_id")
    if isinstance(value, str) and states.get(value) == required_state:
        if consumed is None or value not in consumed:
            return value
        return None
    candidates = [
        request_id
        for request_id, state in states.items()
        if state == required_state and (consumed is None or request_id not in consumed)
    ]
    return candidates[-1] if len(candidates) == 1 else None


def _payload_tool_calls(event: SessionEvent) -> tuple[ToolCallBlock, ...]:
    value = event.payload.get("tool_calls", [])
    if not isinstance(value, list):
        raise InvalidSessionEvents("assistant.message tool_calls must be a list")
    calls: list[ToolCallBlock] = []
    for item in value:
        if not isinstance(item, dict):
            raise InvalidSessionEvents("assistant.message Tool Call must be an object")
        call_id = item.get("tool_call_id")
        name = item.get("name")
        arguments = item.get("arguments", {})
        if not isinstance(call_id, str) or not call_id.strip():
            raise InvalidSessionEvents("assistant.message Tool Call ID is required")
        if not isinstance(name, str) or not name.strip():
            raise InvalidSessionEvents("assistant.message Tool name is required")
        if not isinstance(arguments, dict):
            raise InvalidSessionEvents("assistant.message Tool arguments must be an object")
        calls.append(ToolCallBlock(call_id, name, cast(dict[str, object], arguments)))
    return tuple(calls)


def _payload_object(event: SessionEvent, key: str) -> dict[str, JSONValue]:
    value = event.payload.get(key)
    if not isinstance(value, dict):
        raise InvalidSessionEvents(f"{event.event_type} requires an object {key}")
    return value


def _tool_for_event(
    tool_states: dict[str, ToolCallProjection], event: SessionEvent
) -> ToolCallProjection:
    tool_call_id = _payload_string(event, "tool_call_id")
    try:
        return tool_states[tool_call_id]
    except KeyError as exc:
        raise InvalidSessionEvents(f"event refers to unknown Tool Call {tool_call_id!r}") from exc


def _replace_tool(
    calls: tuple[ToolCallProjection, ...], updated: ToolCallProjection
) -> tuple[ToolCallProjection, ...]:
    return tuple(updated if call.tool_call_id == updated.tool_call_id else call for call in calls)


def _payload_string(event: SessionEvent, key: str) -> str:
    value = event.payload.get(key)
    if not isinstance(value, str) or not value.strip():
        raise InvalidSessionEvents(f"{event.event_type} requires a non-blank {key}")
    return value


def _payload_nonnegative_int(event: SessionEvent, key: str) -> int:
    value = event.payload.get(key)
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise InvalidSessionEvents(f"{event.event_type} requires a non-negative {key}")
    return value


def _int_field(record: Mapping[str, object], key: str) -> int:
    value = record.get(key)
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"event {key} must be an integer")
    return value


def _string_field(record: Mapping[str, object], key: str) -> str:
    value = record.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"event {key} must be a non-blank string")
    return value


def _optional_string_field(record: Mapping[str, object], key: str) -> str | None:
    value = record.get(key)
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"event {key} must be a non-blank string or null")
    return value
