"""Serial Fake-driven Agent Turn orchestration for bounded read/search Tools."""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import cast

from mini_agent.application.ports import (
    Clock,
    EventObserver,
    IDGenerator,
    ModelProvider,
    PermissionGate,
    ResumedSession,
    SessionStore,
    SessionWriter,
)
from mini_agent.configuration import ConfigurationResolver, EffectiveConfiguration
from mini_agent.context import ContextBuilder, ContextFrame
from mini_agent.domain.messages import AssistantMessage, Message, ToolResultMessage, UserMessage
from mini_agent.domain.sessions import JSONValue, SessionEvent, SessionEventType
from mini_agent.domain.streams import StreamEvent
from mini_agent.domain.turns import StreamFailed, close_agent_response
from mini_agent.tools.contracts import (
    PermissionDecision,
    RiskAssessment,
    ToolCall,
    ToolOutcome,
    ToolRegistry,
    ToolResult,
    ToolValidationError,
    invalid_result,
)
from mini_agent.tools.workspace import Workspace, WorkspacePathError


@dataclass(frozen=True, slots=True)
class AgentTurnResult:
    """Observable result of a completed serial Tool Turn."""

    session_id: str
    turn_id: str
    user_message: UserMessage
    assistant_message: AssistantMessage
    tool_results: tuple[ToolResultMessage, ...]
    started_at: datetime
    completed_at: datetime
    usage_input_tokens: int
    usage_output_tokens: int
    model_request_count: int
    tool_call_count: int
    stream_events: tuple[StreamEvent, ...]


class ReadOnlyPermissionGate:
    """Automatically allow safe reads and deny other side-effect classes."""

    def decide(self, risk: RiskAssessment) -> PermissionDecision:
        if risk.side_effect.value == "read":
            return PermissionDecision.ALLOW
        return PermissionDecision.DENY


SafeReadPermissionGate = ReadOnlyPermissionGate


class AgentTurnApplication:
    """Run one bounded, serial model/Tool loop through durable Session Events."""

    def __init__(
        self,
        provider: ModelProvider,
        workspace: Workspace,
        tool_registry: ToolRegistry,
        clock: Clock,
        id_generator: IDGenerator,
        session_store: SessionStore | None = None,
        *,
        context_builder: ContextBuilder | None = None,
        configuration: EffectiveConfiguration | None = None,
        configuration_resolver: ConfigurationResolver | None = None,
        request_targets: tuple[str, ...] = (),
        permission_gate: PermissionGate | None = None,
    ) -> None:
        self._provider = provider
        self._workspace = workspace
        self._tool_registry = tool_registry
        self._clock = clock
        self._id_generator = id_generator
        self._session_store = session_store
        self._context_builder = context_builder
        self._configuration = configuration
        self._configuration_resolver = configuration_resolver
        self._request_targets = request_targets
        self._permission_gate = permission_gate or ReadOnlyPermissionGate()

    async def run(
        self,
        task: str,
        on_event: EventObserver | None = None,
        *,
        session_id: str | None = None,
    ) -> AgentTurnResult:
        user_message = UserMessage(task)
        requested_session_id = session_id
        if requested_session_id is not None and self._session_store is None:
            raise ValueError("session_id requires a Session Store")
        resolved_session_id = session_id or self._id_generator.new_id("session")
        turn_id = self._id_generator.new_id("turn")
        started_at = self._clock.now()
        writer: SessionWriter | None = None
        resumed: ResumedSession | None = None
        history: tuple[Message, ...] = ()
        conversation: tuple[Message, ...]
        all_stream_events: list[StreamEvent] = []
        tool_messages: list[ToolResultMessage] = []
        input_tokens = 0
        output_tokens = 0
        request_count = 0
        tool_count = 0
        effective_configuration = self._configuration

        try:
            if self._session_store is not None:
                if requested_session_id is not None:
                    resumed = self._session_store.resume(resolved_session_id)
                    history = resumed.messages
                    writer = self._session_store.open_writer(resolved_session_id)
                else:
                    writer = self._session_store.create(
                        resolved_session_id,
                        created_at=started_at,
                    )
                if self._configuration_resolver is not None:
                    effective_configuration = self._configuration_resolver.resolve(
                        session_overrides=(
                            resumed.configuration_overrides if resumed is not None else None
                        )
                    )
                turn_started = writer.append(
                    SessionEventType.TURN_STARTED,
                    {},
                    turn_id=turn_id,
                    timestamp=started_at,
                )
                user_event = writer.append(
                    SessionEventType.USER_MESSAGE,
                    {"role": "user", "content": user_message.content},
                    turn_id=turn_id,
                    causation_id=turn_started.event_id,
                    timestamp=started_at,
                )
            else:
                user_event = None
            conversation = (*history, user_message)
        except BaseException as exc:
            if writer is not None:
                self._record_failed_turn(writer, turn_id, None, exc)
                writer.close()
            raise

        max_requests = effective_configuration.max_model_requests if effective_configuration else 25
        max_tools = effective_configuration.max_tool_calls if effective_configuration else 50
        max_active_seconds = (
            effective_configuration.max_active_seconds if effective_configuration else 30 * 60
        )

        try:
            while request_count < max_requests:
                self._ensure_active_budget(started_at, max_active_seconds)
                request_count += 1
                request_id = self._id_generator.new_id("request")
                frame = self._build_frame(
                    task,
                    request_id=request_id,
                    resolved_session_id=resolved_session_id,
                    history=_history_without_current_user(conversation, history, user_message),
                    configuration=effective_configuration,
                    writer=writer,
                )
                if writer is not None:
                    causation_id = user_event.event_id if user_event is not None else None
                    if frame is not None:
                        manifest_event = writer.append(
                            SessionEventType.CONTEXT_MANIFEST_RECORDED,
                            {
                                "manifest": frame.manifest.as_dict(),
                                "manifest_hash": frame.manifest.manifest_hash_without_self(),
                            },
                            turn_id=turn_id,
                            causation_id=causation_id,
                            timestamp=self._clock.now(),
                        )
                        causation_id = manifest_event.event_id
                    request_event = writer.append(
                        SessionEventType.MODEL_REQUEST_STARTED,
                        {"request_id": request_id, "message_count": len(conversation)},
                        turn_id=turn_id,
                        causation_id=causation_id,
                        timestamp=self._clock.now(),
                    )
                else:
                    request_event = None

                stream_events: list[StreamEvent] = []
                try:
                    provider_input: tuple[Message, ...] | ContextFrame = (
                        frame if frame is not None else conversation
                    )
                    async for event in self._provider.stream(provider_input):
                        stream_events.append(event)
                        all_stream_events.append(event)
                        self._ensure_active_budget(started_at, max_active_seconds)
                        if on_event is not None:
                            observed = on_event(event)
                            if inspect.isawaitable(observed):
                                await observed
                    response = close_agent_response(tuple(stream_events))
                except BaseException as exc:
                    if writer is not None and request_event is not None:
                        writer.append(
                            SessionEventType.MODEL_REQUEST_FAILED,
                            {**_failure_payload(exc), "request_id": request_id},
                            turn_id=turn_id,
                            causation_id=request_event.event_id,
                            timestamp=self._clock.now(),
                        )
                    raise

                input_tokens += response.usage.input_tokens
                output_tokens += response.usage.output_tokens
                if writer is not None and request_event is not None:
                    completed_event = writer.append(
                        SessionEventType.MODEL_REQUEST_COMPLETED,
                        {
                            "request_id": request_id,
                            "input_tokens": response.usage.input_tokens,
                            "output_tokens": response.usage.output_tokens,
                        },
                        turn_id=turn_id,
                        causation_id=request_event.event_id,
                        timestamp=self._clock.now(),
                    )
                    assistant_event = writer.append(
                        SessionEventType.ASSISTANT_MESSAGE,
                        _assistant_payload(response.message),
                        turn_id=turn_id,
                        causation_id=completed_event.event_id,
                        timestamp=self._clock.now(),
                    )
                else:
                    assistant_event = None
                conversation = (*conversation, response.message)

                if not response.message.tool_calls:
                    completed_at = self._clock.now()
                    if writer is not None and assistant_event is not None:
                        writer.append(
                            SessionEventType.TURN_COMPLETED,
                            {
                                "outcome": "completed",
                                "input_tokens": input_tokens,
                                "output_tokens": output_tokens,
                            },
                            turn_id=turn_id,
                            causation_id=assistant_event.event_id,
                            timestamp=completed_at,
                        )
                    return AgentTurnResult(
                        session_id=resolved_session_id,
                        turn_id=turn_id,
                        user_message=user_message,
                        assistant_message=response.message,
                        tool_results=tuple(tool_messages),
                        started_at=started_at,
                        completed_at=completed_at,
                        usage_input_tokens=input_tokens,
                        usage_output_tokens=output_tokens,
                        model_request_count=request_count,
                        tool_call_count=tool_count,
                        stream_events=tuple(all_stream_events),
                    )

                for block in response.message.tool_calls:
                    if tool_count >= max_tools:
                        raise AgentLimitError("Tool Call budget exhausted")
                    tool_count += 1
                    call = ToolCall(
                        tool_call_id=block.tool_call_id,
                        name=block.name,
                        arguments=block.arguments,
                    )
                    proposed_event = self._append_tool_event(
                        writer,
                        SessionEventType.TOOL_PROPOSED,
                        {
                            "tool_call_id": call.tool_call_id,
                            "name": call.name,
                            "arguments": cast(dict[str, JSONValue], call.arguments),
                        },
                        turn_id=turn_id,
                        causation_id=assistant_event.event_id if assistant_event else None,
                    )
                    result, terminal_causation = await self._execute_tool(
                        call,
                        writer,
                        turn_id=turn_id,
                        causation_id=proposed_event.event_id if proposed_event else None,
                        permission_mode=(
                            effective_configuration.permission_mode.value
                            if effective_configuration is not None
                            else "read-only"
                        ),
                    )
                    terminal_event = self._append_tool_terminal(
                        writer,
                        result,
                        turn_id=turn_id,
                        causation_id=terminal_causation,
                    )
                    del terminal_event
                    result_message = ToolResultMessage(
                        call.tool_call_id,
                        result.text,
                        result.outcome.value,
                    )
                    tool_messages.append(result_message)
                    conversation = (*conversation, result_message)
                    if result.outcome is ToolOutcome.INTERRUPTED:
                        raise asyncio.CancelledError
                continue
            raise AgentLimitError("model request budget exhausted")
        except BaseException as exc:
            if writer is not None:
                self._record_failed_turn(writer, turn_id, None, exc)
            raise
        finally:
            if writer is not None:
                writer.close()

    def _build_frame(
        self,
        task: str,
        *,
        request_id: str,
        resolved_session_id: str,
        history: tuple[Message, ...],
        configuration: EffectiveConfiguration | None,
        writer: SessionWriter | None,
    ) -> ContextFrame | None:
        if self._context_builder is None:
            return None
        selected_events: list[Mapping[str, object]] = []
        if writer is not None:
            selected_events = [
                {"type": event.event_type, **_tool_event_summary(event)}
                for event in writer.events
                if event.event_type.startswith("tool.")
            ]
        return self._context_builder.build(
            task,
            request_id=request_id,
            session_id=resolved_session_id,
            targets=self._request_targets,
            history=history,
            configuration=configuration,
            tool_definitions=[
                definition.model_dump(mode="json")
                for definition in self._tool_registry.definitions()
            ],
            selected_events=selected_events,
            included_event_range=(1, len(writer.events)) if writer is not None else None,
        )

    async def _execute_tool(
        self,
        call: ToolCall,
        writer: SessionWriter | None,
        *,
        turn_id: str,
        causation_id: str | None,
        permission_mode: str,
    ) -> tuple[ToolResult, str | None]:
        try:
            validated = self._tool_registry.validate(call)
        except ToolValidationError:
            return invalid_result(
                call, code="invalid-input", message="Tool arguments are invalid"
            ), causation_id
        tool = self._tool_registry.require(call.name)
        preflight_failure: ToolResult | None = None
        preflight = getattr(tool, "preflight", None)
        try:
            if preflight is not None:
                preflight(self._workspace, validated.arguments)
        except WorkspacePathError as exc:
            preflight_failure = ToolResult.failed(
                call,
                category="tool-execution",
                code=exc.code,
                message=str(exc),
            )
        decision = (
            PermissionDecision.DENY
            if preflight_failure is not None
            else self._permission_gate.decide(validated.risk)
        )
        decision_at = self._clock.now()
        validated_event = self._append_tool_event(
            writer,
            SessionEventType.TOOL_VALIDATED,
            {
                "tool_call_id": call.tool_call_id,
                "name": call.name,
                "arguments": cast(dict[str, JSONValue], call.arguments),
                "risk": cast(dict[str, JSONValue], validated.risk.model_dump(mode="json")),
                "permission": _permission_payload(
                    call,
                    validated.risk,
                    decision,
                    mode=permission_mode,
                    matched_rule="workspace-confinement" if preflight_failure else None,
                    reason=preflight_failure.text if preflight_failure else None,
                    timestamp=decision_at,
                ),
            },
            turn_id=turn_id,
            causation_id=causation_id,
            timestamp=decision_at,
        )
        if preflight_failure is not None:
            return (
                preflight_failure,
                validated_event.event_id if validated_event else causation_id,
            )
        if decision is not PermissionDecision.ALLOW:
            return (
                ToolResult.failed(
                    call,
                    outcome=ToolOutcome.DENIED,
                    category="permission",
                    code="read-only-policy",
                    message="Tool operation was denied by the host Permission Policy",
                ),
                validated_event.event_id if validated_event else causation_id,
            )
        started_event = self._append_tool_event(
            writer,
            SessionEventType.TOOL_STARTED,
            {"tool_call_id": call.tool_call_id, "name": call.name},
            turn_id=turn_id,
            causation_id=validated_event.event_id if validated_event else causation_id,
        )
        try:
            result = await asyncio.wait_for(
                tool.execute(self._workspace, validated.arguments),
                timeout=tool.limits.timeout_seconds,
            )
            return result.for_call(call), started_event.event_id if started_event else causation_id
        except TimeoutError:
            return (
                ToolResult.failed(
                    call,
                    category="tool-execution",
                    code="timeout",
                    message="Tool execution exceeded its time limit",
                ),
                started_event.event_id if started_event else causation_id,
            )
        except asyncio.CancelledError:
            return (
                ToolResult.failed(
                    call,
                    outcome=ToolOutcome.INTERRUPTED,
                    category="cancellation",
                    code="tool-interrupted",
                    message="Tool execution was interrupted before its side effects were known",
                ),
                started_event.event_id if started_event else causation_id,
            )
        except Exception:
            return (
                ToolResult.failed(
                    call,
                    category="tool-execution",
                    code="execution-failed",
                    message="Tool execution failed",
                ),
                started_event.event_id if started_event else causation_id,
            )

    def _ensure_active_budget(self, started_at: datetime, max_active_seconds: int) -> None:
        elapsed = (self._clock.now() - started_at).total_seconds()
        if elapsed >= max_active_seconds:
            raise AgentLimitError("active execution budget exhausted")

    def _append_tool_event(
        self,
        writer: SessionWriter | None,
        event_type: SessionEventType,
        payload: Mapping[str, JSONValue],
        *,
        turn_id: str,
        causation_id: str | None,
        timestamp: datetime | None = None,
    ) -> SessionEvent | None:
        if writer is None:
            return None
        return writer.append(
            event_type,
            payload,
            turn_id=turn_id,
            causation_id=causation_id,
            timestamp=timestamp or self._clock.now(),
        )

    def _append_tool_terminal(
        self,
        writer: SessionWriter | None,
        result: ToolResult,
        *,
        turn_id: str,
        causation_id: str | None,
    ) -> SessionEvent | None:
        if writer is None:
            return None
        event_type = (
            SessionEventType.TOOL_COMPLETED
            if result.outcome is ToolOutcome.SUCCESS
            else SessionEventType.TOOL_INTERRUPTED
            if result.outcome is ToolOutcome.INTERRUPTED
            else SessionEventType.TOOL_FAILED
        )
        payload = cast(
            dict[str, JSONValue],
            {
                "tool_call_id": result.tool_call_id,
                "name": result.tool_name,
                "outcome": result.outcome.value,
                "result": cast(dict[str, JSONValue], result.model_dump(mode="json")),
                "result_text": result.text,
            },
        )
        return writer.append(
            event_type,
            payload,
            turn_id=turn_id,
            causation_id=causation_id,
            timestamp=self._clock.now(),
        )

    def _record_failed_turn(
        self,
        writer: SessionWriter,
        turn_id: str,
        request_event: SessionEvent | None,
        exc: BaseException,
    ) -> None:
        try:
            causation_id = request_event.event_id if request_event is not None else None
            writer.append(
                SessionEventType.TURN_FAILED,
                {**_failure_payload(exc), "outcome": "failed"},
                turn_id=turn_id,
                causation_id=causation_id,
                timestamp=self._clock.now(),
            )
        except Exception:
            return


class AgentLimitError(RuntimeError):
    """Raised when the host safety budget prevents more model/tool work."""


def _history_without_current_user(
    conversation: tuple[Message, ...],
    prior_history: tuple[Message, ...],
    user_message: UserMessage,
) -> tuple[Message, ...]:
    """Keep the active task in the ContextFrame's current-user layer only."""

    index = len(prior_history)
    if index < len(conversation) and conversation[index] is user_message:
        return (*conversation[:index], *conversation[index + 1 :])
    return conversation


def _permission_payload(
    call: ToolCall,
    risk: RiskAssessment,
    decision: PermissionDecision,
    *,
    mode: str,
    matched_rule: str | None,
    reason: str | None,
    timestamp: datetime,
) -> dict[str, JSONValue]:
    arguments = json.dumps(
        {"name": call.name, "arguments": call.arguments},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    allowed = decision is PermissionDecision.ALLOW
    return {
        "tool_call_id": call.tool_call_id,
        "risk": cast(dict[str, JSONValue], risk.model_dump(mode="json")),
        "mode": mode,
        "decision": decision.value,
        "matched_rule": matched_rule or ("safe-read" if allowed else "read-only-deny"),
        "reason": reason
        or (
            "safe Workspace read automatically authorized"
            if allowed
            else "non-read Tool Call denied by the read-only host policy"
        ),
        "scope": "turn",
        "resource_summary": list(risk.resources),
        "argument_hash": hashlib.sha256(arguments).hexdigest(),
        "timestamp": timestamp.isoformat(),
    }


def _assistant_payload(message: AssistantMessage) -> dict[str, JSONValue]:
    return {
        "role": "assistant",
        "content": message.content,
        "tool_calls": [
            {
                "tool_call_id": call.tool_call_id,
                "name": call.name,
                "arguments": cast(dict[str, JSONValue], call.arguments),
            }
            for call in message.tool_calls
        ],
    }


def _tool_event_summary(event: SessionEvent) -> dict[str, object]:
    return {
        key: value
        for key, value in event.payload.items()
        if key in {"tool_call_id", "name", "outcome", "result_text"}
    }


def _failure_payload(exc: BaseException) -> dict[str, str]:
    if isinstance(exc, StreamFailed):
        failure = exc.event.failure
        return {
            "category": failure.category,
            "source": failure.source,
            "description": failure.redacted_description,
        }
    return {
        "category": "agent",
        "source": "application",
        "description": f"{type(exc).__name__}: {str(exc)[:200]}",
    }
