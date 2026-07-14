from datetime import UTC, datetime

import pytest

from mini_agent.adapters.clocks import DeterministicClock
from mini_agent.adapters.ids import DeterministicIdGenerator
from mini_agent.application.turns import TextTurnApplication
from mini_agent.domain.messages import UserMessage
from mini_agent.domain.streams import (
    ResponseCompleted,
    ResponseStarted,
    TextDelta,
    UsageReported,
)
from mini_agent.providers.fake import ScriptedFakeModelProvider


@pytest.mark.asyncio
async def test_scripted_provider_completes_a_text_turn_through_application_ports() -> None:
    provider = ScriptedFakeModelProvider(chunks=("A small ", "answer."))
    application = TextTurnApplication(
        provider=provider,
        clock=DeterministicClock(datetime(2026, 1, 1, tzinfo=UTC)),
        id_generator=DeterministicIdGenerator(),
    )
    events = []

    result = await application.run("Explain Mini Agent", on_event=events.append)

    assert result.session_id == "session-0001"
    assert result.turn_id == "turn-0001"
    assert result.user_message == UserMessage("Explain Mini Agent")
    assert result.assistant_message.content == "A small answer."
    assert result.started_at == datetime(2026, 1, 1, tzinfo=UTC)
    assert result.completed_at == datetime(2026, 1, 1, 0, 0, 0, 1, tzinfo=UTC)
    assert [type(event) for event in events] == [
        ResponseStarted,
        TextDelta,
        TextDelta,
        UsageReported,
        ResponseCompleted,
    ]
    assert provider.requests == [(UserMessage("Explain Mini Agent"),)]


@pytest.mark.asyncio
async def test_application_supports_async_stream_observers() -> None:
    provider = ScriptedFakeModelProvider(chunks=("streamed",))
    application = TextTurnApplication(
        provider=provider,
        clock=DeterministicClock(datetime(2026, 1, 1, tzinfo=UTC)),
        id_generator=DeterministicIdGenerator(),
    )
    observed = []

    async def observe(event: object) -> None:
        observed.append(event)

    result = await application.run("Give one word", on_event=observe)

    assert result.assistant_message.content == "streamed"
    assert len(observed) == 4
