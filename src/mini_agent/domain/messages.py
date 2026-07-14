"""Provider-neutral messages exchanged by the Agent Loop."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal


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
    """A completed text response from the Model Provider."""

    content: str
    role: Literal["assistant"] = "assistant"

    def __post_init__(self) -> None:
        if not self.content:
            raise ValueError("assistant message content cannot be empty")


type Message = UserMessage | AssistantMessage
