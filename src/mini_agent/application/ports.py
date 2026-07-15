"""Replaceable Application Ports used by the first text-only Turn."""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable, Mapping, Sequence
from datetime import datetime
from typing import Protocol

from mini_agent.context import ContextFrame
from mini_agent.domain.messages import Message
from mini_agent.domain.sessions import JSONValue, SessionEvent, SessionEventType
from mini_agent.domain.streams import StreamEvent


class ModelProvider(Protocol):
    """Boundary through which the Agent Loop receives model responses."""

    def stream(
        self, messages: Sequence[Message] | ContextFrame
    ) -> AsyncIterator[StreamEvent]:
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


class SessionWriter(Protocol):
    """The durable-before-side-effect seam used by the Turn application."""

    def append(
        self,
        event_type: str | SessionEventType,
        payload: Mapping[str, JSONValue],
        *,
        turn_id: str | None = None,
        causation_id: str | None = None,
        timestamp: datetime | None = None,
        event_id: str | None = None,
    ) -> SessionEvent:
        """Append one event and return its durable identity."""

    def close(self) -> None:
        """Release the exclusive Session writer."""


class ResumedSession(Protocol):
    """Rebuilt durable state used to assemble the next provider request."""

    @property
    def session_id(self) -> str:
        """Stable Session identity."""

    @property
    def messages(self) -> tuple[Message, ...]:
        """Complete durable messages in context order."""

    @property
    def configuration_overrides(self) -> Mapping[str, JSONValue]:
        """Allowlisted non-secret Session configuration state."""


class ContextBuilder(Protocol):
    """Boundary that derives a fresh Context Frame per model request."""

    def build(self, user_message: str, **kwargs: object) -> ContextFrame:
        """Assemble one typed Context Frame."""


class InstructionLoader(Protocol):
    """Boundary for path-scoped project instruction discovery."""

    def load(self, targets: Sequence[str] = ()) -> object:
        """Load effective AGENTS.md instructions for target paths."""


class SessionStore(Protocol):
    """Persistence boundary for text-only Session lifecycle events."""

    def create(
        self,
        session_id: str | None = None,
        *,
        created_at: datetime | None = None,
    ) -> SessionWriter:
        """Create a Session and record its root event."""

    def open_writer(self, session_id: str) -> SessionWriter:
        """Open the one exclusive writer for an existing Session."""

    def resume(self, session_id: str) -> ResumedSession:
        """Rebuild a Session from its event history."""
