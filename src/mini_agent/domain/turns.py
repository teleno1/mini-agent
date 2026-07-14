"""Rules for closing one text-only streamed response into a Turn message."""

from __future__ import annotations

from dataclasses import dataclass

from mini_agent.domain.messages import AssistantMessage
from mini_agent.domain.streams import (
    ResponseCompleted,
    ResponseFailed,
    ResponseStarted,
    StreamEvent,
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
