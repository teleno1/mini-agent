"""The first application use case: run one text-only Turn."""

from __future__ import annotations

import inspect
from dataclasses import dataclass
from datetime import datetime

from mini_agent.application.ports import Clock, EventObserver, IDGenerator, ModelProvider
from mini_agent.domain.messages import AssistantMessage, UserMessage
from mini_agent.domain.streams import StreamEvent
from mini_agent.domain.turns import close_text_response


@dataclass(frozen=True, slots=True)
class TurnResult:
    """Observable result of a successfully closed text-only Turn."""

    session_id: str
    turn_id: str
    user_message: UserMessage
    assistant_message: AssistantMessage
    started_at: datetime
    completed_at: datetime
    usage_input_tokens: int
    usage_output_tokens: int
    stream_events: tuple[StreamEvent, ...]


class TextTurnApplication:
    """Orchestrate a single provider request through explicit Application Ports."""

    def __init__(self, provider: ModelProvider, clock: Clock, id_generator: IDGenerator) -> None:
        self._provider = provider
        self._clock = clock
        self._id_generator = id_generator

    async def run(self, task: str, on_event: EventObserver | None = None) -> TurnResult:
        user_message = UserMessage(task)
        session_id = self._id_generator.new_id("session")
        turn_id = self._id_generator.new_id("turn")
        started_at = self._clock.now()
        events: list[StreamEvent] = []

        async for event in self._provider.stream((user_message,)):
            events.append(event)
            if on_event is not None:
                observed = on_event(event)
                if inspect.isawaitable(observed):
                    await observed

        response = close_text_response(tuple(events))
        return TurnResult(
            session_id=session_id,
            turn_id=turn_id,
            user_message=user_message,
            assistant_message=response.message,
            started_at=started_at,
            completed_at=self._clock.now(),
            usage_input_tokens=response.usage.input_tokens,
            usage_output_tokens=response.usage.output_tokens,
            stream_events=tuple(events),
        )
