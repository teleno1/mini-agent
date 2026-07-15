"""Offline scripted Model Provider used by tests and the first CLI journey."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Sequence

from mini_agent.context import ContextFrame
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
        responses: Sequence[Sequence[StreamEvent]] | None = None,
        scripts: Sequence[Sequence[StreamEvent]] | None = None,
    ) -> None:
        if responses is not None and scripts is not None:
            raise ValueError("provide responses or scripts, not both")
        self._chunks = tuple(chunks)
        self._request_id = request_id
        self._usage = usage or UsageReported(input_tokens=1, output_tokens=len(self._chunks))
        self._responses = tuple(tuple(response) for response in (responses or scripts or ()))
        self.requests: list[tuple[Message, ...] | ContextFrame] = []

    def stream(self, messages: Sequence[Message] | ContextFrame) -> AsyncIterator[StreamEvent]:
        return self._stream(messages if isinstance(messages, ContextFrame) else tuple(messages))

    async def _stream(
        self, messages: tuple[Message, ...] | ContextFrame
    ) -> AsyncIterator[StreamEvent]:
        self.requests.append(messages)
        if self._responses:
            response = self._responses[min(len(self.requests) - 1, len(self._responses) - 1)]
            for event in response:
                await asyncio.sleep(0)
                yield event
            return
        yield ResponseStarted(request_id=self._request_id)
        for chunk in self._chunks:
            await asyncio.sleep(0)
            yield TextDelta(text=chunk)
        yield self._usage
        yield ResponseCompleted()
