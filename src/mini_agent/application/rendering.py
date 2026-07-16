"""Bounded, provider-neutral stream rendering with a plain-text fallback."""

from __future__ import annotations

import asyncio
import inspect
from collections.abc import Awaitable, Callable

from mini_agent.domain.streams import ResponseCompleted, ResponseFailed, StreamEvent, TextDelta

type TextSink = Callable[[str], Awaitable[object] | object]
type _QueueItem = StreamEvent | None


class BoundedStreamRenderer:
    """Consume stream events with backpressure and coalesced text refreshes.

    ``observe`` awaits a bounded queue put, so a slow renderer naturally
    applies upstream backpressure. Text deltas are joined only for display;
    ``aggregate_text`` is updated one delta at a time and is never changed by
    coalescing or renderer failures.
    """

    def __init__(
        self,
        *,
        rich_sink: TextSink | None = None,
        plain_sink: TextSink | None = None,
        max_queue_size: int = 64,
    ) -> None:
        if max_queue_size < 1:
            raise ValueError("renderer queue size must be positive")
        self._rich_sink = rich_sink
        self._plain_sink = plain_sink
        self._queue: asyncio.Queue[_QueueItem] = asyncio.Queue(maxsize=max_queue_size)
        self._worker: asyncio.Task[None] | None = None
        self._pending: _QueueItem = None
        self._has_pending = False
        self._closed = False
        self._rich_failed = rich_sink is None
        self._aggregate_text = ""
        self._incomplete = False
        self._completed = False
        self._fallback_used = False

    @property
    def aggregate_text(self) -> str:
        return self._aggregate_text

    @property
    def incomplete(self) -> bool:
        return self._incomplete

    @property
    def completed(self) -> bool:
        return self._completed

    @property
    def fallback_used(self) -> bool:
        return self._fallback_used

    async def start(self) -> None:
        if self._closed:
            raise RuntimeError("renderer is already closed")
        if self._worker is None:
            self._worker = asyncio.create_task(self._consume(), name="mini-agent-renderer")

    async def observe(self, event: StreamEvent) -> None:
        if self._closed:
            raise RuntimeError("renderer is already closed")
        if isinstance(event, TextDelta):
            self._aggregate_text += event.text
        elif isinstance(event, ResponseFailed):
            self._incomplete = True
        elif isinstance(event, ResponseCompleted):
            self._completed = True
        await self.start()
        await self._queue.put(event)

    async def finish(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._worker is None:
            return
        await self._queue.put(None)
        await self._worker

    async def wait_idle(self) -> None:
        """Wait until all observed stream events have reached the sink."""

        if self._worker is not None:
            await self._queue.join()

    async def __aenter__(self) -> BoundedStreamRenderer:
        await self.start()
        return self

    async def __aexit__(self, exc_type: object, exc_value: object, traceback: object) -> None:
        del exc_type, exc_value, traceback
        await self.finish()

    async def _consume(self) -> None:
        while True:
            if self._has_pending:
                item = self._pending
                self._has_pending = False
            else:
                item = await self._queue.get()
            if item is None:
                self._queue.task_done()
                return
            try:
                if isinstance(item, TextDelta):
                    chunks = [item.text]
                    while True:
                        try:
                            next_item = self._queue.get_nowait()
                        except asyncio.QueueEmpty:
                            break
                        if isinstance(next_item, TextDelta):
                            chunks.append(next_item.text)
                            self._queue.task_done()
                        else:
                            self._pending = next_item
                            self._has_pending = True
                            break
                    await self._emit("".join(chunks))
                elif isinstance(item, ResponseFailed):
                    await self._emit(" [stream incomplete]")
            finally:
                self._queue.task_done()

    async def _emit(self, text: str) -> None:
        sink = self._plain_sink if self._rich_failed else self._rich_sink
        if sink is None:
            return
        try:
            result = sink(text)
            if inspect.isawaitable(result):
                await result
        except Exception:
            if self._rich_failed:
                return
            self._rich_failed = True
            self._fallback_used = True
            if self._plain_sink is not None:
                try:
                    result = self._plain_sink(text)
                    if inspect.isawaitable(result):
                        await result
                except Exception:
                    return


class PlainTextRenderer(BoundedStreamRenderer):
    """Named convenience wrapper for non-ANSI output."""

    def __init__(self, sink: TextSink | None = None, *, max_queue_size: int = 64) -> None:
        super().__init__(plain_sink=sink, max_queue_size=max_queue_size)


StreamRenderer = BoundedStreamRenderer


__all__ = ["BoundedStreamRenderer", "PlainTextRenderer", "StreamRenderer"]
