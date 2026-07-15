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

from mini_agent.domain.messages import AssistantMessage, Message, UserMessage

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


class SessionStatus(StrEnum):
    """Rebuildable user-visible Session status."""

    IDLE = "idle"
    ACTIVE = "active"
    COMPLETED = "completed"
    FAILED = "failed"


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
            if turn.request_started:
                raise InvalidSessionEvents(f"Turn {turn.turn_id!r} started two model requests")
            turns[turn.turn_id] = replace(turn, request_started=True)
        elif event_type is SessionEventType.MODEL_REQUEST_COMPLETED:
            if not turn.request_started or turn.request_completed or turn.request_failed:
                raise InvalidSessionEvents(
                    f"Turn {turn.turn_id!r} has an out-of-order model request completion"
                )
            turns[turn.turn_id] = replace(
                turn,
                usage_input_tokens=_payload_nonnegative_int(event, "input_tokens"),
                usage_output_tokens=_payload_nonnegative_int(event, "output_tokens"),
                request_completed=True,
            )
        elif event_type is SessionEventType.MODEL_REQUEST_FAILED:
            if not turn.request_started or turn.request_completed or turn.request_failed:
                raise InvalidSessionEvents(
                    f"Turn {turn.turn_id!r} has an out-of-order model request failure"
                )
            turns[turn.turn_id] = replace(turn, request_failed=True)
        elif event_type is SessionEventType.ASSISTANT_MESSAGE:
            if not turn.request_completed:
                raise InvalidSessionEvents(
                    f"Turn {turn.turn_id!r} has an assistant message before request completion"
                )
            content = _payload_string(event, "content")
            assistant_message = AssistantMessage(content)
            if turn.assistant_message is not None:
                raise InvalidSessionEvents(f"Turn {turn.turn_id!r} has two assistant messages")
            turns[turn.turn_id] = replace(turn, assistant_message=assistant_message)
            messages.append(assistant_message)
        elif event_type is SessionEventType.TURN_COMPLETED:
            if (
                turn.user_message is None
                or turn.assistant_message is None
                or not turn.request_completed
            ):
                raise InvalidSessionEvents(f"Turn {turn.turn_id!r} completed without messages")
            turns[turn.turn_id] = replace(
                turn,
                status=SessionStatus.COMPLETED,
                completed_at=event.timestamp,
            )
            status = SessionStatus.COMPLETED
        elif event_type is SessionEventType.TURN_FAILED:
            if turn.request_started and not (turn.request_completed or turn.request_failed):
                raise InvalidSessionEvents(
                    f"Turn {turn.turn_id!r} failed before its model request reached "
                    "a terminal state"
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
