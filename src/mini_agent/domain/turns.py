"""Rules for closing one text-only streamed response into a Turn message."""

from __future__ import annotations

import json
from dataclasses import dataclass

from mini_agent.domain.messages import AssistantMessage, ToolCallBlock
from mini_agent.domain.streams import (
    ResponseCompleted,
    ResponseFailed,
    ResponseStarted,
    StreamEvent,
    ToolCallArgumentDelta,
    ToolCallCompleted,
    ToolCallStarted,
    TextDelta,
    UsageReported,
)


class InvalidStream(ValueError):
    """Raised when a Provider violates the normalized stream contract."""


class StreamFailed(RuntimeError):
    """Raised when a Provider reports a failed response."""

    def __init__(self, event: ResponseFailed) -> None:
        super().__init__(event.failure.redacted_description)
        self.event = event


@dataclass(frozen=True, slots=True)
class TextResponse:
    """The durable text and usage extracted from a valid response stream."""

    message: AssistantMessage
    usage: UsageReported


@dataclass(frozen=True, slots=True)
class AgentResponse:
    """A valid streamed response, optionally requesting structured Tools."""

    message: AssistantMessage
    usage: UsageReported
    stop_reason: str


def close_text_response(events: tuple[StreamEvent, ...]) -> TextResponse:
    """Validate and aggregate a provider stream without knowing its provider."""

    if not events or not isinstance(events[0], ResponseStarted):
        raise InvalidStream("response must start with response-started")

    text_parts: list[str] = []
    usage = UsageReported(input_tokens=0, output_tokens=0)
    usage_reported = False
    completed = False

    for event in events[1:]:
        if completed:
            raise InvalidStream("response cannot emit events after response-completed")
        if isinstance(event, TextDelta):
            if usage_reported:
                raise InvalidStream("text-delta cannot arrive after usage-reported")
            text_parts.append(event.text)
        elif isinstance(event, (ToolCallStarted, ToolCallArgumentDelta, ToolCallCompleted)):
            raise InvalidStream("text-only response cannot contain Tool Calls")
        elif isinstance(event, UsageReported):
            if usage_reported:
                raise InvalidStream("response cannot report usage twice")
            usage = event
            usage_reported = True
        elif isinstance(event, ResponseCompleted):
            completed = True
        elif isinstance(event, ResponseFailed):
            raise StreamFailed(event)
        elif isinstance(event, ResponseStarted):
            raise InvalidStream("response cannot start twice")
        else:
            raise InvalidStream(f"unsupported stream event: {type(event).__name__}")

    if not completed:
        raise InvalidStream("response must end with response-completed")

    return TextResponse(message=AssistantMessage("".join(text_parts)), usage=usage)


def close_agent_response(events: tuple[StreamEvent, ...]) -> AgentResponse:
    """Validate a structured response and discard no partial Tool arguments."""

    if not events or not isinstance(events[0], ResponseStarted):
        raise InvalidStream("response must start with response-started")
    text_parts: list[str] = []
    usage = UsageReported(input_tokens=0, output_tokens=0)
    usage_reported = False
    completed = False
    active: dict[str, tuple[str, list[str]]] = {}
    calls: list[ToolCallBlock] = []
    stop_reason = "stop"

    for event in events[1:]:
        if completed:
            raise InvalidStream("response cannot emit events after response-completed")
        if isinstance(event, TextDelta):
            if usage_reported:
                raise InvalidStream("text-delta cannot arrive after usage-reported")
            text_parts.append(event.text)
        elif isinstance(event, ToolCallStarted):
            if usage_reported or event.tool_call_id in active or any(
                call.tool_call_id == event.tool_call_id for call in calls
            ):
                raise InvalidStream("Tool Call started out of order or twice")
            active[event.tool_call_id] = (event.name, [])
        elif isinstance(event, ToolCallArgumentDelta):
            if usage_reported or event.tool_call_id not in active:
                raise InvalidStream("Tool Call arguments require an active Tool Call")
            active[event.tool_call_id][1].append(event.arguments)
        elif isinstance(event, ToolCallCompleted):
            if usage_reported or event.tool_call_id not in active:
                raise InvalidStream("Tool Call completed without a start")
            name, fragments = active.pop(event.tool_call_id)
            argument_text = "".join(fragments)
            if event.arguments is not None:
                if argument_text:
                    raise InvalidStream("Tool Call cannot mix complete and delta arguments")
                arguments = event.arguments
            else:
                try:
                    decoded = json.loads(argument_text or "{}")
                except json.JSONDecodeError as exc:
                    raise InvalidStream("Tool Call arguments are not valid JSON") from exc
                if not isinstance(decoded, dict):
                    raise InvalidStream("Tool Call arguments must be a JSON object")
                arguments = decoded
            calls.append(
                ToolCallBlock(
                    tool_call_id=event.tool_call_id,
                    name=name,
                    arguments=arguments,
                )
            )
        elif isinstance(event, UsageReported):
            if usage_reported or active:
                raise InvalidStream("usage cannot arrive before all Tool Calls complete")
            usage = event
            usage_reported = True
        elif isinstance(event, ResponseCompleted):
            if active:
                raise InvalidStream("response completed with an incomplete Tool Call")
            if calls and event.stop_reason != "tool_calls":
                raise InvalidStream("Tool Call response requires tool_calls stop reason")
            if not calls and event.stop_reason != "stop":
                raise InvalidStream("text response requires stop stop reason")
            stop_reason = event.stop_reason
            completed = True
        elif isinstance(event, ResponseFailed):
            raise StreamFailed(event)
        elif isinstance(event, ResponseStarted):
            raise InvalidStream("response cannot start twice")
        else:
            raise InvalidStream(f"unsupported stream event: {type(event).__name__}")

    if not completed:
        raise InvalidStream("response must end with response-completed")
    return AgentResponse(
        message=AssistantMessage("".join(text_parts), tuple(calls)),
        usage=usage,
        stop_reason=stop_reason,
    )
