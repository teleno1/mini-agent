"""Provider-neutral messages exchanged by the Agent Loop."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal


@dataclass(frozen=True, slots=True)
class ToolCallBlock:
    """A complete structured Tool Call embedded in an Assistant Message."""

    tool_call_id: str
    name: str
    arguments: dict[str, Any]

    @property
    def id(self) -> str:
        return self.tool_call_id


@dataclass(frozen=True, slots=True)
class ToolResultMessage:
    """A model-facing observation linked to exactly one Tool Call."""

    tool_call_id: str
    content: str
    outcome: str
    role: Literal["tool"] = "tool"

    def __post_init__(self) -> None:
        if not self.tool_call_id.strip():
            raise ValueError("Tool Result Message requires a Tool Call ID")


@dataclass(frozen=True, slots=True)
class UserMessage:
    """A task submitted by the user."""

    content: str
    role: Literal["user"] = "user"

    def __post_init__(self) -> None:
        if not self.content.strip():
            raise ValueError("user message content cannot be blank")


@dataclass(frozen=True, slots=True)
class AssistantMessage:
    """A completed response with ordered text and optional structured calls."""

    content: str
    tool_calls: tuple[ToolCallBlock, ...] = ()
    role: Literal["assistant"] = "assistant"

    def __post_init__(self) -> None:
        if not self.content and not self.tool_calls:
            raise ValueError("assistant message must contain text or Tool Calls")


type Message = UserMessage | AssistantMessage | ToolResultMessage
