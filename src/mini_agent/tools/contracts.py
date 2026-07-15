"""Typed, UI-independent contracts for host-enforced Tools."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Protocol, runtime_checkable

from pydantic import AliasChoices, BaseModel, ConfigDict, Field, ValidationError

from mini_agent.tools.workspace import Workspace


class SideEffectCategory(StrEnum):
    """The broad side-effect class used by a Permission Policy."""

    READ = "read"
    WRITE = "write"
    EXECUTE = "execute"
    DANGEROUS = "dangerous"


class ToolOutcome(StrEnum):
    """Business outcomes returned by a Tool execution."""

    SUCCESS = "success"
    INVALID = "invalid"
    DENIED = "denied"
    FAILED = "failed"
    CANCELLED = "cancelled"
    INTERRUPTED = "interrupted"


class PermissionDecision(StrEnum):
    """A host authorization result; Tools never prompt users themselves."""

    ALLOW = "allow"
    ASK = "ask"
    DENY = "deny"


class CancellationBehavior(StrEnum):
    """How promptly a Tool can stop after its awaiting task is cancelled."""

    BEST_EFFORT = "best-effort"
    COOPERATIVE = "cooperative"


class ToolLimits(BaseModel):
    """Cancellation and output limits advertised with a Tool definition."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    timeout_seconds: float = 30.0
    max_output_bytes: int = 64 * 1024
    cancellation: CancellationBehavior = CancellationBehavior.BEST_EFFORT

    @classmethod
    def bounded(cls, *, timeout_seconds: float, max_output_bytes: int) -> ToolLimits:
        return cls(timeout_seconds=timeout_seconds, max_output_bytes=max_output_bytes)


class RiskAssessment(BaseModel):
    """Pure metadata about a proposed call, with no authority to execute it."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    side_effect: SideEffectCategory
    resources: tuple[str, ...] = ()
    hazards: tuple[str, ...] = ()
    summary: str


class ToolCall(BaseModel):
    """An immutable model-proposed Tool invocation."""

    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True)

    tool_call_id: str = Field(
        validation_alias=AliasChoices("tool_call_id", "id", "call_id"),
        min_length=1,
    )
    name: str = Field(min_length=1)
    arguments: dict[str, Any] = Field(default_factory=dict)

    @property
    def id(self) -> str:
        """Convenience spelling used by provider-neutral message code."""

        return self.tool_call_id


@dataclass(frozen=True, slots=True)
class NormalizedToolCall:
    """Deeply immutable canonical Tool Call presented to permission policy."""

    tool_call_id: str
    name: str
    arguments_json: str

    @classmethod
    def from_call(cls, call: ToolCall) -> NormalizedToolCall:
        arguments_json = json.dumps(
            call.arguments,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        return cls(call.tool_call_id, call.name, arguments_json)

    @property
    def arguments(self) -> dict[str, Any]:
        """Return a disposable copy without exposing mutable authorization state."""

        value = json.loads(self.arguments_json)
        if not isinstance(value, dict):
            raise TypeError("normalized Tool arguments must be an object")
        return value

    @property
    def argument_hash(self) -> str:
        payload = json.dumps(
            {"name": self.name, "arguments": self.arguments},
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return hashlib.sha256(payload).hexdigest()


@dataclass(frozen=True, slots=True)
class PermissionRequest:
    """Immutable normalized call and risk metadata considered by a Permission Gate."""

    call: NormalizedToolCall
    risk: RiskAssessment


class ToolError(BaseModel):
    """Bounded, redacted error details returned to the model."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    category: str
    code: str
    message: str


class ToolResult(BaseModel):
    """One bounded observation linked to exactly one Tool Call."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    tool_call_id: str = Field(min_length=1)
    tool_name: str = Field(min_length=1)
    outcome: ToolOutcome
    data: dict[str, Any] = Field(default_factory=dict)
    error: ToolError | None = None

    @property
    def success(self) -> bool:
        return self.outcome is ToolOutcome.SUCCESS

    @property
    def text(self) -> str:
        """Stable JSON text suitable for a Tool Result Message."""

        if self.error is not None:
            return self.error.message
        if not self.data:
            return ""
        return json.dumps(self.data, ensure_ascii=False, sort_keys=True)

    def for_call(self, call: ToolCall) -> ToolResult:
        """Bind a Tool implementation's bounded result to its immutable call."""

        if call.name != self.tool_name:
            raise ValueError("Tool Result name does not match Tool Call")
        return self.model_copy(update={"tool_call_id": call.tool_call_id})

    @classmethod
    def succeeded(cls, call: ToolCall, data: Mapping[str, Any] | None = None) -> ToolResult:
        return cls(
            tool_call_id=call.tool_call_id,
            tool_name=call.name,
            outcome=ToolOutcome.SUCCESS,
            data=dict(data or {}),
        )

    @classmethod
    def failed(
        cls,
        call: ToolCall,
        *,
        outcome: ToolOutcome = ToolOutcome.FAILED,
        category: str,
        code: str,
        message: str,
        data: Mapping[str, Any] | None = None,
    ) -> ToolResult:
        if outcome is ToolOutcome.SUCCESS:
            raise ValueError("a failed Tool Result cannot have success outcome")
        return cls(
            tool_call_id=call.tool_call_id,
            tool_name=call.name,
            outcome=outcome,
            data=dict(data or {}),
            error=ToolError(category=category, code=code, message=message),
        )


class ToolDefinition(BaseModel):
    """Provider-neutral schema metadata for a Tool."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str
    description: str
    side_effect: SideEffectCategory
    input_schema: dict[str, Any]
    limits: ToolLimits


class ToolValidationError(ValueError):
    """Raised when a Tool name or input schema is invalid."""

    def __init__(self, message: str, *, call: ToolCall | None = None) -> None:
        super().__init__(message)
        self.call = call


@runtime_checkable
class Tool(Protocol):
    """The narrow contract implemented by a host Tool."""

    name: str
    description: str
    side_effect: SideEffectCategory
    input_model: type[BaseModel]
    limits: ToolLimits

    def assess(self, arguments: BaseModel) -> RiskAssessment:
        """Purely assess risk; this method must not read or mutate the host."""

    async def execute(self, workspace: Workspace, arguments: BaseModel) -> ToolResult:
        """Execute validated input inside the already-selected Workspace."""


class ValidatedToolCall(BaseModel):
    """Validated input and immutable risk metadata for one Tool Call."""

    model_config = ConfigDict(extra="forbid", frozen=True, arbitrary_types_allowed=True)

    call: ToolCall
    arguments: BaseModel
    risk: RiskAssessment

    def permission_request(self, *, resources: tuple[str, ...] | None = None) -> PermissionRequest:
        risk = (
            self.risk
            if resources is None
            else self.risk.model_copy(update={"resources": resources})
        )
        return PermissionRequest(NormalizedToolCall.from_call(self.call), risk)


class ToolRegistry:
    """Stable-name registry and schema validation boundary for Tools."""

    def __init__(self, tools: Iterable[Tool] = ()) -> None:
        self._tools: dict[str, Tool] = {}
        for tool in tools:
            self.register(tool)

    def register(self, tool: Tool) -> None:
        if not tool.name.strip():
            raise ValueError("Tool name cannot be blank")
        if tool.name in self._tools:
            raise ValueError(f"Tool already registered: {tool.name}")
        self._tools[tool.name] = tool

    def get(self, name: str) -> Tool | None:
        return self._tools.get(name)

    def require(self, name: str) -> Tool:
        tool = self.get(name)
        if tool is None:
            raise ToolValidationError(f"unknown Tool: {name}")
        return tool

    def definitions(self) -> tuple[ToolDefinition, ...]:
        return tuple(
            ToolDefinition(
                name=tool.name,
                description=tool.description,
                side_effect=tool.side_effect,
                input_schema=tool.input_model.model_json_schema(),
                limits=tool.limits,
            )
            for tool in self._tools.values()
        )

    def validate(self, call: ToolCall) -> ValidatedToolCall:
        tool = self.require(call.name)
        try:
            arguments = tool.input_model.model_validate(call.arguments)
        except ValidationError as exc:
            raise ToolValidationError(f"invalid arguments for Tool {call.name}", call=call) from exc
        normalized_call = ToolCall(
            tool_call_id=call.tool_call_id,
            name=call.name,
            arguments=arguments.model_dump(mode="json"),
        )
        risk = tool.assess(arguments)
        return ValidatedToolCall(call=normalized_call, arguments=arguments, risk=risk)

    async def execute(self, workspace: Workspace, call: ToolCall) -> ToolResult:
        """Validate and execute one call, preserving its correlation ID."""

        validated = self.validate(call)
        tool = self.require(call.name)
        result = await tool.execute(workspace, validated.arguments)
        return result.for_call(call)


def invalid_result(call: ToolCall, *, code: str, message: str) -> ToolResult:
    """Create a schema/registry failure without invoking a Tool."""

    return ToolResult.failed(
        call,
        outcome=ToolOutcome.INVALID,
        category="tool-validation",
        code=code,
        message=message,
    )
