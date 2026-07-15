"""Discoverable Session Event vocabulary."""

from mini_agent.domain.sessions import (
    CURRENT_SCHEMA_VERSION,
    SUPPORTED_SCHEMA_VERSIONS,
    InvalidSessionEvents,
    JSONValue,
    SessionEvent,
    SessionEventType,
)

EventType = SessionEventType
SCHEMA_VERSION = CURRENT_SCHEMA_VERSION

__all__ = [
    "CURRENT_SCHEMA_VERSION",
    "SUPPORTED_SCHEMA_VERSIONS",
    "InvalidSessionEvents",
    "JSONValue",
    "SCHEMA_VERSION",
    "SessionEvent",
    "SessionEventType",
    "EventType",
]
