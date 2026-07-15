"""Normalized, provider-independent streaming events."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Literal


class StreamEventKind(StrEnum):
    RESPONSE_STARTED = "response-started"
    TEXT_DELTA = "text-delta"
    TOOL_CALL_STARTED = "tool-call-started"
    TOOL_CALL_ARGUMENT_DELTA = "tool-call-argument-delta"
    TOOL_CALL_COMPLETED = "tool-call-completed"
    USAGE_REPORTED = "usage-reported"
    RESPONSE_COMPLETED = "response-completed"
    RESPONSE_FAILED = "response-failed"


@dataclass(frozen=True, slots=True)
class ResponseStarted:
    request_id: str
    kind: Literal[StreamEventKind.RESPONSE_STARTED] = StreamEventKind.RESPONSE_STARTED

    def __post_init__(self) -> None:
        if not self.request_id.strip():
            raise ValueError("response request ID cannot be blank")


@dataclass(frozen=True, slots=True)
class TextDelta:
    text: str
    kind: Literal[StreamEventKind.TEXT_DELTA] = StreamEventKind.TEXT_DELTA


@dataclass(frozen=True, slots=True)
class ToolCallStarted:
    """Start of one structured Tool Call in a streamed response."""

    tool_call_id: str
    name: str
    kind: Literal[StreamEventKind.TOOL_CALL_STARTED] = StreamEventKind.TOOL_CALL_STARTED

    def __post_init__(self) -> None:
        if not self.tool_call_id.strip() or not self.name.strip():
            raise ValueError("Tool Call start requires an ID and name")

    @property
    def id(self) -> str:
        return self.tool_call_id


@dataclass(frozen=True, slots=True)
class ToolCallArgumentDelta:
    """A JSON argument fragment for an active Tool Call."""

    tool_call_id: str
    arguments: str
    kind: Literal[StreamEventKind.TOOL_CALL_ARGUMENT_DELTA] = (
        StreamEventKind.TOOL_CALL_ARGUMENT_DELTA
    )

    def __post_init__(self) -> None:
        if not self.tool_call_id.strip():
            raise ValueError("Tool Call argument delta requires an ID")

    @property
    def id(self) -> str:
        return self.tool_call_id


@dataclass(frozen=True, slots=True)
class ToolCallCompleted:
    """End of one Tool Call's argument stream."""

    tool_call_id: str
    arguments: dict[str, Any] | None = None
    kind: Literal[StreamEventKind.TOOL_CALL_COMPLETED] = StreamEventKind.TOOL_CALL_COMPLETED

    def __post_init__(self) -> None:
        if not self.tool_call_id.strip():
            raise ValueError("Tool Call completion requires an ID")

    @property
    def id(self) -> str:
        return self.tool_call_id


@dataclass(frozen=True, slots=True)
class UsageReported:
    input_tokens: int
    output_tokens: int
    kind: Literal[StreamEventKind.USAGE_REPORTED] = StreamEventKind.USAGE_REPORTED

    def __post_init__(self) -> None:
        if self.input_tokens < 0 or self.output_tokens < 0:
            raise ValueError("token usage cannot be negative")


@dataclass(frozen=True, slots=True)
class ResponseCompleted:
    stop_reason: Literal["stop", "tool_calls"] = "stop"
    kind: Literal[StreamEventKind.RESPONSE_COMPLETED] = StreamEventKind.RESPONSE_COMPLETED


@dataclass(frozen=True, slots=True)
class ResponseFailed:
    failure: Failure
    kind: Literal[StreamEventKind.RESPONSE_FAILED] = StreamEventKind.RESPONSE_FAILED


@dataclass(frozen=True, slots=True)
class Failure:
    """A stable, redacted description of an unsuccessful operation."""

    category: str
    source: str
    redacted_description: str
    retryable: bool
    required_user_action: str
    cause: str | None = None
    code: str = "provider-error"
    retry_after_seconds: float | None = None

    def __post_init__(self) -> None:
        if not all(
            value.strip()
            for value in (
                self.category,
                self.source,
                self.redacted_description,
                self.required_user_action,
                self.code,
            )
        ):
            raise ValueError("failure fields cannot be blank")
        if self.cause is not None and not self.cause.strip():
            raise ValueError("failure cause cannot be blank when provided")
        if self.retry_after_seconds is not None and self.retry_after_seconds < 0:
            raise ValueError("retry-after duration cannot be negative")


type StreamEvent = (
    ResponseStarted
    | TextDelta
    | ToolCallStarted
    | ToolCallArgumentDelta
    | ToolCallCompleted
    | UsageReported
    | ResponseCompleted
    | ResponseFailed
)
