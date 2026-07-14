"""Offline scripted Model Provider used by tests and the first CLI journey."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Sequence

from mini_agent.domain.messages import Message
from mini_agent.domain.streams import (
    ResponseCompleted,
    ResponseStarted,
    StreamEvent,
    TextDelta,
    UsageReported,
)

DEFAULT_CHUNKS = (
    "Mini Agent is a small, ",
    "inspectable coding agent.",
)


class ScriptedFakeModelProvider:
    """Emit a predictable, asynchronous text response without network access."""

    def __init__(
        self,
        chunks: Sequence[str] = DEFAULT_CHUNKS,
        *,
        request_id: str = "fake-request-0001",
        usage: UsageReported | None = None,
    ) -> None:
        self._chunks = tuple(chunks)
        self._request_id = request_id
        self._usage = usage or UsageReported(input_tokens=1, output_tokens=len(self._chunks))
        self.requests: list[tuple[Message, ...]] = []

    def stream(self, messages: Sequence[Message]) -> AsyncIterator[StreamEvent]:
        return self._stream(tuple(messages))

    async def _stream(self, messages: tuple[Message, ...]) -> AsyncIterator[StreamEvent]:
        self.requests.append(messages)
        yield ResponseStarted(request_id=self._request_id)
        for chunk in self._chunks:
            await asyncio.sleep(0)
            yield TextDelta(text=chunk)
        yield self._usage
        yield ResponseCompleted()
