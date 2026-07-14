"""Replaceable Application Ports used by the first text-only Turn."""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from datetime import datetime
from typing import Protocol

from mini_agent.domain.messages import Message
from mini_agent.domain.streams import StreamEvent


class ModelProvider(Protocol):
    """Boundary through which the Agent Loop receives model responses."""

    def stream(self, messages: Sequence[Message]) -> AsyncIterator[StreamEvent]:
        """Stream normalized events for one provider request."""


class Clock(Protocol):
    """Boundary for wall-clock time used in durable Turn metadata."""

    def now(self) -> datetime:
        """Return the current timezone-aware instant."""


class IDGenerator(Protocol):
    """Boundary for identifiers used to correlate Sessions and Turns."""

    def new_id(self, namespace: str) -> str:
        """Return a new identifier in the requested namespace."""


type EventObserver = Callable[[StreamEvent], Awaitable[None] | None]
