"""Normalized, provider-independent streaming events."""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Literal


class FailureCategory(StrEnum):
    """Stable categories shared by Providers, Tools, and the Agent Loop."""

    CONFIGURATION = "configuration"
    AUTHENTICATION = "authentication"
    RATE_LIMIT = "rate-limit"
    NETWORK = "network"
    PROVIDER_TIMEOUT = "provider-timeout"
    PROVIDER_PROTOCOL = "provider-protocol"
    CONTEXT_OVERFLOW = "context-overflow"
    PERMISSION_DENIAL = "permission-denial"
    TOOL_VALIDATION = "tool-validation"
    TOOL_EXECUTION = "tool-execution"
    TOOL_TIMEOUT = "tool-timeout"
    PERSISTENCE = "persistence"
    CANCELLATION = "cancellation"
    INTERNAL = "internal"


STABLE_FAILURE_CATEGORIES = frozenset(item.value for item in FailureCategory)


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
    details: Mapping[str, object] = field(default_factory=dict)
    failure_id: str | None = None
    session_id: str | None = None
    turn_id: str | None = None
    request_id: str | None = None
    tool_call_id: str | None = None

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
        if self.category not in STABLE_FAILURE_CATEGORIES:
            raise ValueError(f"unknown Failure category: {self.category}")
        object.__setattr__(self, "redacted_description", _redact_text(self.redacted_description))
        object.__setattr__(self, "required_user_action", _redact_text(self.required_user_action))
        if self.cause is not None:
            object.__setattr__(self, "cause", _redact_text(self.cause))
        object.__setattr__(self, "details", _redact_details(self.details))
        for name, value in (
            ("failure ID", self.failure_id),
            ("Session ID", self.session_id),
            ("Turn ID", self.turn_id),
            ("request ID", self.request_id),
            ("Tool Call ID", self.tool_call_id),
        ):
            if value is not None and not value.strip():
                raise ValueError(f"{name} cannot be blank when provided")

    @property
    def error_id(self) -> str | None:
        """The concise identifier shown to a user and accepted by ``doctor``."""

        return self.failure_id

    @property
    def redacted_details(self) -> Mapping[str, object]:
        """Alias making the no-secrets contract explicit at call sites."""

        return self.details

    @property
    def correlation_ids(self) -> dict[str, str]:
        """Return the non-empty identifiers that locate this failure."""

        return {
            name: value
            for name, value in {
                "failure_id": self.failure_id,
                "session_id": self.session_id,
                "turn_id": self.turn_id,
                "request_id": self.request_id,
                "tool_call_id": self.tool_call_id,
            }.items()
            if value is not None
        }

    def as_dict(self) -> dict[str, object]:
        """Return the bounded, non-secret diagnostic/event representation."""

        return {
            "category": self.category,
            "code": self.code,
            "description": _redact_text(self.redacted_description),
            "details": dict(self.details),
            "source": self.source,
            "retryable": self.retryable,
            "required_user_action": self.required_user_action,
            "cause": self.cause,
            "retry_after_seconds": self.retry_after_seconds,
            "failure_id": self.failure_id,
            "error_id": self.failure_id,
            "session_id": self.session_id,
            "turn_id": self.turn_id,
            "request_id": self.request_id,
            "tool_call_id": self.tool_call_id,
        }


_SENSITIVE_DETAIL_KEYS = frozenset(
    {
        "api_key",
        "apikey",
        "authorization",
        "credential",
        "password",
        "prompt",
        "raw_output",
        "secret",
        "stderr",
        "stdout",
        "system_prompt",
        "token",
        "tool_output",
    }
)


def _redact_text(value: str) -> str:
    redacted = re.sub(r"(?i)(bearer\s+)[A-Za-z0-9._~+/=-]+", r"\1<redacted>", value)
    redacted = re.sub(
        r"(?i)(api[_ -]?key\s*[:=]\s*)[^\s,;]+",
        r"\1<redacted>",
        redacted,
    )
    return re.sub(
        r"\b(?:sk|pk|ghp|gho|github_pat|xox[baprs])[-_][A-Za-z0-9_-]{8,}\b", "<redacted>", redacted
    )


def _redact_details(value: Mapping[str, object]) -> dict[str, object]:
    def redact(item: object, key: str | None = None) -> object:
        if key is not None and key.casefold().replace("-", "_") in _SENSITIVE_DETAIL_KEYS:
            return "<omitted>"
        if isinstance(item, str):
            return _redact_text(item[:4096])
        if isinstance(item, Mapping):
            return {
                str(raw_key): redact(raw_value, str(raw_key)) for raw_key, raw_value in item.items()
            }
        if isinstance(item, (list, tuple)):
            return [redact(child) for child in item[:100]]
        return item

    return redact(value)  # type: ignore[return-value]


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
