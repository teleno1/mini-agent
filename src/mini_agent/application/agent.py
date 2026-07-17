"""Serial Fake-driven Agent Turn orchestration for bounded read/search Tools."""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import os
import random
import threading
import warnings
from collections.abc import Awaitable, Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import datetime
from typing import Any, cast

from mini_agent.application.permissions import PermissionPolicyGate, UserInteraction
from mini_agent.application.ports import (
    Clock,
    EventObserver,
    IDGenerator,
    LifecycleObserver,
    ModelProvider,
    PermissionGate,
    ResumedSession,
    SessionStore,
    SessionWriter,
)
from mini_agent.configuration import (
    ConfigurationResolver,
    EffectiveConfiguration,
    PermissionMode,
    redact_secrets,
)
from mini_agent.context import ContextBudgetError, ContextBuilder, ContextFrame
from mini_agent.diagnostics import DiagnosticLogger, failure_from_exception
from mini_agent.domain.artifacts import ARTIFACT_MEDIA_TYPE, ARTIFACT_PREVIEW_BYTES
from mini_agent.domain.compaction import (
    ContextCompactionError,
    ContextCompactor,
    ContextSummary,
    TokenEstimator,
    messages_after_boundary,
    response_reserve_tokens,
)
from mini_agent.domain.messages import AssistantMessage, Message, ToolResultMessage, UserMessage
from mini_agent.domain.plans import PlanSnapshot, PlanStep, PlanStepStatus
from mini_agent.domain.reports import CompletionReport
from mini_agent.domain.sessions import JSONValue, SessionEvent, SessionEventType
from mini_agent.domain.streams import (
    Failure,
    StreamEvent,
    TextDelta,
    ToolCallArgumentDelta,
    ToolCallCompleted,
    ToolCallStarted,
)
from mini_agent.domain.turns import close_agent_response
from mini_agent.tools.contracts import (
    MAX_TOOL_RESPONSE_BYTES,
    PermissionDecision,
    PermissionRequest,
    ToolCall,
    ToolError,
    ToolOutcome,
    ToolRegistry,
    ToolResult,
    ToolValidationError,
    invalid_result,
)
from mini_agent.tools.patches import WriteValidationError
from mini_agent.tools.workspace import Workspace, WorkspacePathError


@dataclass(frozen=True, slots=True)
class TurnBudgets:
    """Host limits for one Agent Turn.

    The model-request, Tool-call, and active-time defaults mirror the MVP
    specification.  Token, rendered-output, and pre-output retry limits are
    kept here so the application loop cannot accidentally become unbounded;
    callers may lower them for a smaller or more defensive host.
    """

    max_model_requests: int = 25
    max_tool_calls: int = 50
    max_active_seconds: int = 30 * 60
    max_total_tokens: int = 1_000_000
    max_output_bytes: int = 1_024 * 1_024
    max_retries: int = 2

    def __post_init__(self) -> None:
        if self.max_model_requests > 25 or self.max_tool_calls > 50:
            raise ValueError("Turn budgets exceed the host safety ceiling")
        if self.max_active_seconds > 30 * 60:
            raise ValueError("active execution budget exceeds the host safety ceiling")
        if self.max_total_tokens > 1_000_000 or self.max_output_bytes > 1_024 * 1_024:
            raise ValueError("output budget exceeds the host safety ceiling")
        if self.max_retries > 2:
            raise ValueError("retry budget exceeds the host safety ceiling")
        for name in (
            "max_model_requests",
            "max_tool_calls",
            "max_total_tokens",
            "max_output_bytes",
        ):
            value = getattr(self, name)
            if isinstance(value, bool) or value < 1:
                raise ValueError(f"{name} must be at least one")
        if isinstance(self.max_active_seconds, bool) or self.max_active_seconds < 0:
            raise ValueError("max_active_seconds cannot be negative")
        if isinstance(self.max_retries, bool) or self.max_retries < 0:
            raise ValueError("max_retries cannot be negative")


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
    completion_report: CompletionReport


@dataclass(frozen=True, slots=True)
class RecoveryRetryResult:
    """Result of explicit retries executed as newly validated Tool calls."""

    session_id: str
    turn_id: str
    old_tool_call_ids: tuple[str, ...]
    new_tool_call_ids: tuple[str, ...]
    tool_results: tuple[ToolResult, ...]
    completed_at: datetime


class ReadOnlyPermissionGate:
    """Automatically allow safe reads and deny other side-effect classes."""

    def decide(self, request: PermissionRequest) -> PermissionDecision:
        if request.risk.side_effect.value == "read":
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
        user_interaction: UserInteraction | None = None,
        budgets: TurnBudgets | None = None,
        context_compactor: ContextCompactor | None = None,
        retry_sleep: Callable[[float], Awaitable[object]] = asyncio.sleep,
        diagnostic_logger: DiagnosticLogger | None = None,
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
        self._permission_gate = permission_gate
        self._default_permission_gate = PermissionPolicyGate(
            PermissionMode.SUGGEST,
            interaction=user_interaction,
        )
        self._budgets = budgets
        self._token_estimator = TokenEstimator()
        self._context_compactor = context_compactor or ContextCompactor(self._token_estimator)
        self._active_sessions: set[str] = set()
        self._active_sessions_lock = threading.Lock()
        self._retry_sleep = retry_sleep
        self._diagnostic_logger = diagnostic_logger or DiagnosticLogger(
            workspace.root,
            id_generator=id_generator,
        )

    def _turn_budgets(self, configuration: EffectiveConfiguration | None) -> TurnBudgets:
        if self._budgets is not None:
            return self._budgets
        return TurnBudgets(
            max_model_requests=(
                configuration.max_model_requests if configuration is not None else 25
            ),
            max_tool_calls=configuration.max_tool_calls if configuration is not None else 50,
            max_active_seconds=(
                configuration.max_active_seconds if configuration is not None else 30 * 60
            ),
        )

    def _claim_active_session(self, session_id: str) -> None:
        with self._active_sessions_lock:
            if session_id in self._active_sessions:
                raise AgentTurnError("Session already has an active Turn")
            self._active_sessions.add(session_id)

    def _release_active_session(self, session_id: str) -> None:
        with self._active_sessions_lock:
            self._active_sessions.discard(session_id)

    async def run(
        self,
        task: str,
        on_event: EventObserver | None = None,
        *,
        session_id: str | None = None,
        on_lifecycle: LifecycleObserver | None = None,
    ) -> AgentTurnResult:
        user_message = UserMessage(task)
        requested_session_id = session_id
        if requested_session_id is not None and self._session_store is None:
            raise ValueError("session_id requires a Session Store")
        resolved_session_id = session_id or self._id_generator.new_id("session")
        self._claim_active_session(resolved_session_id)
        turn_id = self._id_generator.new_id("turn")
        started_at = self._clock.now()
        writer: SessionWriter | None = None
        resumed: ResumedSession | None = None
        history: tuple[Message, ...] = ()
        context_history: tuple[Message, ...] = ()
        context_summary: ContextSummary | None = None
        summary_boundary = 0
        conversation: tuple[Message, ...]
        all_stream_events: list[StreamEvent] = []
        tool_messages: list[ToolResultMessage] = []
        input_tokens = 0
        output_tokens = 0
        request_count = 0
        tool_count = 0
        retry_count = 0
        output_bytes = 0
        plan: PlanSnapshot | None = None
        tool_observations: list[tuple[ToolCall, ToolResult]] = []
        active_request_event: SessionEvent | None = None
        last_failure: Failure | None = None
        effective_configuration = self._configuration
        plan_mode_enabled = False

        try:
            if self._session_store is not None:
                if requested_session_id is not None:
                    resumed = self._session_store.resume(resolved_session_id)
                    context_summary = getattr(resumed, "context_summary", None)
                    summary_boundary = getattr(resumed, "summary_boundary", 0)
                    history = (
                        messages_after_boundary(resumed.events, summary_boundary)
                        if context_summary is not None
                        else resumed.messages
                    )
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
            plan_mode_enabled = bool(
                effective_configuration.plan_mode if effective_configuration is not None else False
            )
            conversation = (*history, user_message)
            context_history = history
            budgets = self._turn_budgets(effective_configuration)
        except BaseException as exc:
            failure = self._report_failure(
                exc,
                session_id=resolved_session_id,
                turn_id=turn_id,
            )
            if writer is not None:
                self._record_failed_turn(writer, turn_id, active_request_event, failure)
                writer.close()
            self._release_active_session(resolved_session_id)
            raise

        permission_gate = self._permission_gate or self._default_permission_gate
        try:
            if isinstance(permission_gate, PermissionPolicyGate):
                permission_gate.begin_session(resolved_session_id)
                permission_gate.set_mode(
                    effective_configuration.permission_mode
                    if effective_configuration is not None
                    else PermissionMode.SUGGEST
                )
            execution_workspace = (
                self._workspace.for_session(resolved_session_id)
                if writer is not None
                else self._workspace
            )
        except BaseException as exc:
            failure = self._report_failure(
                exc,
                session_id=resolved_session_id,
                turn_id=turn_id,
            )
            if writer is not None:
                self._record_failed_turn(writer, turn_id, None, failure)
                writer.close()
            self._release_active_session(resolved_session_id)
            raise

        try:
            while request_count < budgets.max_model_requests:
                self._ensure_active_budget(started_at, budgets.max_active_seconds)
                request_count += 1
                request_id = self._id_generator.new_id("request")
                frame, context_history, context_summary, summary_boundary = self._build_frame(
                    task,
                    request_id=request_id,
                    resolved_session_id=resolved_session_id,
                    history=tuple(
                        message for message in context_history if message is not user_message
                    ),
                    current_user_event=user_event,
                    configuration=effective_configuration,
                    writer=writer,
                    plan=plan,
                    context_summary=context_summary,
                    summary_boundary=summary_boundary,
                    on_lifecycle=on_lifecycle,
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
                    active_request_event = request_event
                    _notify_lifecycle(
                        on_lifecycle,
                        SessionEventType.MODEL_REQUEST_STARTED.value,
                        request_event.payload,
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
                        self._ensure_active_budget(started_at, budgets.max_active_seconds)
                        output_bytes += _stream_output_bytes(event)
                        if output_bytes > budgets.max_output_bytes:
                            raise AgentLimitError("model output budget exhausted")
                        if on_event is not None:
                            try:
                                observed = on_event(event)
                                if inspect.isawaitable(observed):
                                    await observed
                            except Exception:
                                # Rendering is an observation path.  A broken
                                # stdout sink must not change durable model
                                # semantics or trigger a false retry.
                                pass
                    response = close_agent_response(tuple(stream_events))
                except BaseException as exc:
                    failure = self._report_failure(
                        exc,
                        session_id=resolved_session_id,
                        turn_id=turn_id,
                        request_id=request_id,
                    )
                    last_failure = failure
                    if writer is not None and request_event is not None:
                        failure_payload = cast(dict[str, JSONValue], failure.as_dict())
                        writer.append(
                            SessionEventType.MODEL_REQUEST_FAILED,
                            {**failure_payload, "request_id": request_id},
                            turn_id=turn_id,
                            causation_id=request_event.event_id,
                            timestamp=self._clock.now(),
                        )
                        _notify_lifecycle(
                            on_lifecycle,
                            SessionEventType.MODEL_REQUEST_FAILED.value,
                            failure_payload,
                        )
                        active_request_event = None
                    if (
                        _is_safe_provider_retry(failure)
                        and not _stream_has_model_output(stream_events)
                        and retry_count < budgets.max_retries
                    ):
                        _notify_lifecycle(
                            on_lifecycle,
                            "model.request.retrying",
                            {
                                "attempt": retry_count + 2,
                                "max_attempts": budgets.max_retries + 1,
                                "reason": failure.redacted_description,
                            },
                        )
                        await self._wait_before_retry(failure, retry_count)
                        retry_count += 1
                        continue
                    raise

                input_tokens += response.usage.input_tokens
                output_tokens += response.usage.output_tokens
                if frame is not None:
                    self._token_estimator.calibrate_with_usage(
                        max(1, self._token_estimator.estimate_context(frame)), response.usage
                    )
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
                    active_request_event = None
                    assistant_event = writer.append(
                        SessionEventType.ASSISTANT_MESSAGE,
                        _assistant_payload(response.message),
                        turn_id=turn_id,
                        causation_id=completed_event.event_id,
                        timestamp=self._clock.now(),
                    )
                    _notify_lifecycle(
                        on_lifecycle,
                        SessionEventType.MODEL_REQUEST_COMPLETED.value,
                        completed_event.payload,
                    )
                else:
                    assistant_event = None
                conversation = (*conversation, response.message)
                context_history = (*context_history, response.message)

                if input_tokens + output_tokens > budgets.max_total_tokens:
                    raise AgentLimitError("token usage budget exhausted")

                if not response.message.tool_calls:
                    report = build_completion_report(tool_observations)
                    plan_event: SessionEvent | None = None
                    if plan is not None:
                        plan = _finish_plan(plan, report, self._clock.now())
                        plan_event = self._append_plan_event(
                            writer,
                            plan,
                            turn_id=turn_id,
                            causation_id=assistant_event.event_id if assistant_event else None,
                        )
                    completed_at = self._clock.now()
                    if writer is not None and assistant_event is not None:
                        writer.append(
                            SessionEventType.TURN_COMPLETED,
                            {
                                "outcome": "completed",
                                "input_tokens": input_tokens,
                                "output_tokens": output_tokens,
                                "model_request_count": request_count,
                                "tool_call_count": tool_count,
                                "report": report.as_dict(),
                            },
                            turn_id=turn_id,
                            causation_id=(
                                plan_event.event_id if plan_event else assistant_event.event_id
                            ),
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
                        completion_report=report,
                    )

                if (
                    plan_mode_enabled
                    and plan is None
                    and _requires_plan(task, response.message.tool_calls)
                ):
                    plan = _new_plan(
                        task,
                        response.message.tool_calls,
                        self._id_generator.new_id("plan"),
                        self._clock.now(),
                    )
                    self._append_plan_event(
                        writer,
                        plan,
                        turn_id=turn_id,
                        causation_id=assistant_event.event_id if assistant_event else None,
                    )
                    _notify_lifecycle(
                        on_lifecycle,
                        SessionEventType.PLAN_UPDATED.value,
                        {"plan": plan.as_dict()},
                    )

                for block in response.message.tool_calls:
                    if tool_count >= budgets.max_tool_calls:
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
                    _notify_lifecycle(
                        on_lifecycle,
                        SessionEventType.TOOL_PROPOSED.value,
                        {
                            "tool_call_id": call.tool_call_id,
                            "name": call.name,
                            "arguments": cast(dict[str, JSONValue], call.arguments),
                        },
                    )
                    result, terminal_causation = await self._execute_tool(
                        call,
                        writer,
                        execution_workspace,
                        permission_gate,
                        turn_id=turn_id,
                        causation_id=proposed_event.event_id if proposed_event else None,
                        permission_mode=(
                            effective_configuration.permission_mode.value
                            if effective_configuration is not None
                            else "read-only"
                        ),
                        on_lifecycle=on_lifecycle,
                    )
                    result, terminal_causation = self._materialize_tool_result(
                        result,
                        writer,
                        configuration=effective_configuration,
                        max_output_bytes=_max_tool_output_bytes(
                            self._tool_registry,
                            call,
                            result,
                        ),
                        threshold=(
                            effective_configuration.artifact_threshold_bytes
                            if effective_configuration is not None
                            else 32 * 1024
                        ),
                        turn_id=turn_id,
                        causation_id=terminal_causation,
                    )
                    terminal_event = self._append_tool_terminal(
                        writer,
                        result,
                        turn_id=turn_id,
                        causation_id=terminal_causation,
                    )
                    _notify_lifecycle(
                        on_lifecycle,
                        (
                            SessionEventType.TOOL_COMPLETED.value
                            if result.outcome is ToolOutcome.SUCCESS
                            else SessionEventType.TOOL_INTERRUPTED.value
                            if result.outcome is ToolOutcome.INTERRUPTED
                            else SessionEventType.TOOL_FAILED.value
                        ),
                        {
                            "tool_call_id": result.tool_call_id,
                            "name": result.tool_name,
                            "outcome": result.outcome.value,
                            "result_text": result.text,
                            "result": cast(dict[str, JSONValue], result.model_dump(mode="json")),
                        },
                    )
                    execution_workspace.clear_tool_recovery(call.tool_call_id)
                    result_message = ToolResultMessage(
                        call.tool_call_id,
                        result.text,
                        result.outcome.value,
                    )
                    tool_messages.append(result_message)
                    tool_observations.append((call, result))
                    conversation = (*conversation, result_message)
                    context_history = (*context_history, result_message)
                    if plan is not None:
                        plan = _advance_plan(plan, call.name, result, self._clock.now())
                        self._append_plan_event(
                            writer,
                            plan,
                            turn_id=turn_id,
                            causation_id=terminal_event.event_id if terminal_event else None,
                        )
                        _notify_lifecycle(
                            on_lifecycle,
                            SessionEventType.PLAN_UPDATED.value,
                            {"plan": plan.as_dict()},
                        )
                    if result.outcome in {ToolOutcome.INTERRUPTED, ToolOutcome.CANCELLED}:
                        raise asyncio.CancelledError
                continue
            raise AgentLimitError("model request budget exhausted")
        except BaseException as exc:
            failure = last_failure or self._report_failure(
                exc,
                session_id=resolved_session_id,
                turn_id=turn_id,
                request_id=_event_request_id(active_request_event),
                tool_call_id=getattr(exc, "tool_call_id", None),
            )
            if writer is not None:
                self._record_failed_turn(writer, turn_id, active_request_event, failure)
            raise
        finally:
            if writer is not None:
                writer.close()
            self._release_active_session(resolved_session_id)

    async def retry_interrupted(self, session_id: str) -> RecoveryRetryResult:
        """Retry every interrupted call as a fresh, validated, authorized call.

        Resume first closes the old uncertain calls as ``interrupted``.  The
        recovery Turn then proposes new IDs and sends each through the normal
        Tool Registry and Permission Gate.  It does not replay the old call or
        manufacture a Provider response.
        """

        if self._session_store is None:
            raise ValueError("retry_interrupted requires a Session Store")
        reconcile = getattr(self._session_store, "reconcile_resume", None)
        if reconcile is None:
            raise TypeError("the Session Store does not support validated Resume recovery")
        outcome = reconcile(session_id, "retry")
        inspection = outcome.inspection
        resumed = outcome.resumed
        if resumed is None:
            raise AgentTurnError("Resume retry did not close the interrupted Turn")
        effective_configuration = self._configuration
        if self._configuration_resolver is not None:
            effective_configuration = self._configuration_resolver.resolve(
                session_overrides=resumed.configuration_overrides
            )
        permission_gate = self._permission_gate or self._default_permission_gate
        if isinstance(permission_gate, PermissionPolicyGate):
            permission_gate.begin_session(session_id)
            permission_gate.set_mode(
                effective_configuration.permission_mode
                if effective_configuration is not None
                else PermissionMode.SUGGEST
            )

        writer = self._session_store.open_writer(session_id)
        workspace = self._workspace.for_session(session_id)
        turn_id = self._id_generator.new_id("turn")
        started_at = self._clock.now()
        results: list[ToolResult] = []
        new_call_ids: list[str] = []
        try:
            turn_started = writer.append(
                SessionEventType.TURN_STARTED,
                {},
                turn_id=turn_id,
                timestamp=started_at,
            )
            user_event = writer.append(
                SessionEventType.USER_MESSAGE,
                {
                    "role": "user",
                    "content": "Explicitly retry the interrupted Tool calls as new calls.",
                },
                turn_id=turn_id,
                causation_id=turn_started.event_id,
                timestamp=started_at,
            )
            for evidence in inspection.interrupted_tools:
                new_call_id = self._id_generator.new_id("tool")
                new_call_ids.append(new_call_id)
                call = ToolCall(
                    tool_call_id=new_call_id,
                    name=evidence.name,
                    arguments=evidence.arguments,
                )
                proposed = writer.append(
                    SessionEventType.TOOL_PROPOSED,
                    {
                        "tool_call_id": new_call_id,
                        "name": evidence.name,
                        "arguments": cast(dict[str, JSONValue], evidence.arguments),
                    },
                    turn_id=turn_id,
                    causation_id=user_event.event_id,
                )
                result, terminal_causation = await self._execute_tool(
                    call,
                    writer,
                    workspace,
                    permission_gate,
                    turn_id=turn_id,
                    causation_id=proposed.event_id,
                    permission_mode=(
                        effective_configuration.permission_mode.value
                        if effective_configuration is not None
                        else PermissionMode.SUGGEST
                    ),
                )
                result, terminal_causation = self._materialize_tool_result(
                    result,
                    writer,
                    configuration=effective_configuration,
                    max_output_bytes=_max_tool_output_bytes(
                        self._tool_registry,
                        call,
                        result,
                    ),
                    threshold=(
                        effective_configuration.artifact_threshold_bytes
                        if effective_configuration is not None
                        else 32 * 1024
                    ),
                    turn_id=turn_id,
                    causation_id=terminal_causation,
                )
                self._append_tool_terminal(
                    writer,
                    result,
                    turn_id=turn_id,
                    causation_id=terminal_causation,
                )
                workspace.clear_tool_recovery(new_call_id)
                results.append(result)
            completed_at = self._clock.now()
            writer.append(
                SessionEventType.TURN_FAILED,
                {
                    "outcome": "recovery-retry",
                    "tool_call_count": len(results),
                    "note": "Recovery retries do not invent a Provider response.",
                },
                turn_id=turn_id,
                causation_id=user_event.event_id,
                timestamp=completed_at,
            )
            return RecoveryRetryResult(
                session_id=session_id,
                turn_id=turn_id,
                old_tool_call_ids=tuple(item.tool_call_id for item in inspection.interrupted_tools),
                new_tool_call_ids=tuple(new_call_ids),
                tool_results=tuple(results),
                completed_at=completed_at,
            )
        finally:
            writer.close()

    def _build_frame(
        self,
        task: str,
        *,
        request_id: str,
        resolved_session_id: str,
        history: tuple[Message, ...],
        current_user_event: SessionEvent | None,
        configuration: EffectiveConfiguration | None,
        writer: SessionWriter | None,
        plan: PlanSnapshot | None,
        context_summary: ContextSummary | None,
        summary_boundary: int,
        on_lifecycle: LifecycleObserver | None = None,
    ) -> tuple[ContextFrame | None, tuple[Message, ...], ContextSummary | None, int]:
        if self._context_builder is None:
            return None, history, context_summary, summary_boundary

        working_history = tuple(history)
        working_summary = context_summary
        working_boundary = summary_boundary
        last_error: ContextBudgetError | None = None
        for attempt in range(1, 4):
            try:
                frame = self._context_builder.build(
                    task,
                    request_id=request_id,
                    session_id=resolved_session_id,
                    targets=self._request_targets,
                    history=working_history,
                    configuration=configuration,
                    summary=working_summary.as_dict() if working_summary is not None else None,
                    summary_boundary=working_boundary,
                    plan=plan.as_dict() if plan is not None else None,
                    tool_definitions=[
                        definition.model_dump(mode="json")
                        for definition in self._tool_registry.definitions()
                    ],
                    message_sources=_session_message_sources(
                        writer,
                        working_boundary,
                        working_history,
                        current_user_event=current_user_event,
                        summary=working_summary,
                        plan=plan,
                    ),
                    included_event_range=(
                        (working_boundary + 1, len(writer.events))
                        if writer is not None and len(writer.events) > working_boundary
                        else None
                    ),
                )
                self._ensure_context_budget(frame, configuration)
                return frame, working_history, working_summary, working_boundary
            except ContextBudgetError as exc:
                last_error = exc
                if writer is not None:
                    compaction_started = writer.append(
                        SessionEventType.CONTEXT_COMPACTION_STARTED,
                        {"attempt": attempt, "reason": "context-budget"},
                        turn_id=self._active_turn_id(writer),
                        timestamp=self._clock.now(),
                    )
                    _notify_lifecycle(
                        on_lifecycle,
                        SessionEventType.CONTEXT_COMPACTION_STARTED.value,
                        compaction_started.payload,
                    )
                micro_history = self._context_compactor.micro_compact_history(working_history)
                if micro_history != working_history:
                    try:
                        micro_frame = self._context_builder.build(
                            task,
                            request_id=request_id,
                            session_id=resolved_session_id,
                            targets=self._request_targets,
                            history=micro_history,
                            configuration=configuration,
                            summary=(
                                working_summary.as_dict() if working_summary is not None else None
                            ),
                            summary_boundary=working_boundary,
                            plan=plan.as_dict() if plan is not None else None,
                            tool_definitions=[
                                definition.model_dump(mode="json")
                                for definition in self._tool_registry.definitions()
                            ],
                            message_sources=_session_message_sources(
                                writer,
                                working_boundary,
                                micro_history,
                                current_user_event=current_user_event,
                                summary=working_summary,
                                plan=plan,
                            ),
                            included_event_range=(
                                (working_boundary + 1, len(writer.events))
                                if writer is not None and len(writer.events) > working_boundary
                                else None
                            ),
                        )
                        self._ensure_context_budget(micro_frame, configuration)
                    except ContextBudgetError:
                        working_history = micro_history
                    else:
                        if writer is not None:
                            compaction_completed = writer.append(
                                SessionEventType.CONTEXT_COMPACTION_COMPLETED,
                                {
                                    "kind": "micro",
                                    "dropped_messages": len(history) - len(micro_history),
                                },
                                turn_id=self._active_turn_id(writer),
                                timestamp=self._clock.now(),
                            )
                            _notify_lifecycle(
                                on_lifecycle,
                                SessionEventType.CONTEXT_COMPACTION_COMPLETED.value,
                                compaction_completed.payload,
                            )
                        return micro_frame, micro_history, working_summary, working_boundary

                try:
                    selected_events = _selected_context_events(writer, working_boundary)
                    micro_events = self._context_compactor.micro_compact_events(
                        selected_events,
                        writer.events if writer is not None else (),
                        working_boundary,
                    )
                    compaction = self._context_compactor.compact(
                        task,
                        working_history,
                        events=writer.events if writer is not None else (),
                        selected_events=micro_events,
                        plan=plan,
                        artifacts=(writer.projection.artifacts if writer is not None else ()),
                        existing_summary=working_summary,
                        summary_boundary=working_boundary,
                    )
                except (ContextCompactionError, ValueError) as compaction_error:
                    if writer is not None:
                        compaction_failed = writer.append(
                            SessionEventType.CONTEXT_COMPACTION_FAILED,
                            {"attempt": attempt, "error": str(compaction_error)[:500]},
                            turn_id=self._active_turn_id(writer),
                            timestamp=self._clock.now(),
                        )
                        _notify_lifecycle(
                            on_lifecycle,
                            SessionEventType.CONTEXT_COMPACTION_FAILED.value,
                            compaction_failed.payload,
                        )
                    if attempt == 3:
                        raise ContextCompactionError(
                            "context remained oversized after three compaction attempts"
                        ) from compaction_error
                    continue

                working_history = compaction.history
                working_summary = compaction.summary
                working_boundary = compaction.summary_boundary
                if working_summary is None:
                    raise ContextCompactionError("compactor returned no validated summary")
                if writer is not None:
                    compaction_completed = writer.append(
                        SessionEventType.CONTEXT_COMPACTION_COMPLETED,
                        {
                            "kind": "summary",
                            "summary": cast(dict[str, JSONValue], working_summary.as_dict()),
                            "summary_boundary": working_boundary,
                        },
                        turn_id=self._active_turn_id(writer),
                        timestamp=self._clock.now(),
                    )
                    _notify_lifecycle(
                        on_lifecycle,
                        SessionEventType.CONTEXT_COMPACTION_COMPLETED.value,
                        compaction_completed.payload,
                    )
                if attempt == 3:
                    if writer is not None:
                        compaction_failed = writer.append(
                            SessionEventType.CONTEXT_COMPACTION_FAILED,
                            {
                                "attempt": attempt,
                                "error": "summary still exceeds the context budget",
                            },
                            turn_id=self._active_turn_id(writer),
                            timestamp=self._clock.now(),
                        )
                        _notify_lifecycle(
                            on_lifecycle,
                            SessionEventType.CONTEXT_COMPACTION_FAILED.value,
                            compaction_failed.payload,
                        )
                    raise ContextCompactionError(
                        "context remained oversized after three compaction attempts"
                    ) from last_error
        raise ContextCompactionError("context compaction did not converge") from last_error

    @staticmethod
    def _active_turn_id(writer: SessionWriter) -> str | None:
        for event in reversed(writer.events):
            if event.turn_id is not None:
                return event.turn_id
        return None

    def _ensure_context_budget(
        self, frame: ContextFrame, configuration: EffectiveConfiguration | None
    ) -> None:
        window_value = frame.manifest.request_parameters.get("context_window_tokens")
        window = configuration.context_window_tokens if configuration is not None else window_value
        if isinstance(window, bool) or not isinstance(window, int):
            return
        configured_reserve = (
            configuration.response_reserve_tokens
            if configuration is not None
            else frame.manifest.request_parameters.get("response_reserve_tokens")
        )
        reserve = (
            response_reserve_tokens(window, configured_reserve)
            if isinstance(configured_reserve, int) and not isinstance(configured_reserve, bool)
            else response_reserve_tokens(window)
        )
        estimated = self._token_estimator.estimate_context(frame)
        if estimated > window - reserve:
            raise ContextBudgetError(
                f"Context Frame needs {estimated} calibrated tokens but only "
                f"{window - reserve} remain after the response reserve"
            )

    async def _execute_tool(
        self,
        call: ToolCall,
        writer: SessionWriter | None,
        workspace: Workspace,
        permission_gate: PermissionGate,
        *,
        turn_id: str,
        causation_id: str | None,
        permission_mode: str,
        on_lifecycle: LifecycleObserver | None = None,
    ) -> tuple[ToolResult, str | None]:
        try:
            validated = self._tool_registry.validate(call)
        except ToolValidationError as exc:
            message = (
                "Tool name is not registered"
                if exc.code == "unknown-tool"
                else "Tool arguments are invalid"
            )
            return invalid_result(call, code=exc.code, message=message), causation_id
        call = validated.call.model_copy(deep=True)
        tool = self._tool_registry.require(call.name)
        preflight_failure: ToolResult | None = None
        preflight = getattr(tool, "preflight", None)
        normalized_resources = validated.risk.resources
        try:
            if preflight is not None:
                normalized_resources = tuple(preflight(workspace, validated.arguments))
        except (WorkspacePathError, WriteValidationError) as exc:
            preflight_failure = _preflight_failure(call, exc)
        permission_request = validated.permission_request(resources=normalized_resources)
        authorized_argument_hash = permission_request.call.argument_hash
        decision = (
            PermissionDecision.DENY
            if preflight_failure is not None
            else permission_gate.decide(permission_request)
        )
        if inspect.isawaitable(decision):
            try:
                decision = await decision
            except asyncio.CancelledError:
                return (
                    ToolResult.failed(
                        call,
                        outcome=ToolOutcome.CANCELLED,
                        category="cancellation",
                        code="permission-interrupted",
                        message="Tool permission was interrupted before execution",
                    ),
                    causation_id,
                )
        if preflight_failure is None and decision is PermissionDecision.ALLOW:
            if _argument_hash(validated.call) != authorized_argument_hash:
                preflight_failure = ToolResult.failed(
                    call,
                    outcome=ToolOutcome.DENIED,
                    category="permission",
                    code="arguments-changed",
                    message="Tool arguments changed after authorization",
                )
                decision = PermissionDecision.DENY
            else:
                try:
                    if preflight is not None:
                        final_resources = tuple(preflight(workspace, validated.arguments))
                        if final_resources != normalized_resources:
                            raise WorkspacePathError("outside")
                except (WorkspacePathError, WriteValidationError) as exc:
                    preflight_failure = _preflight_failure(call, exc)
                    decision = PermissionDecision.DENY
        decision_at = self._clock.now()
        decision_metadata = getattr(permission_gate, "last_metadata", None)
        validated_event = self._append_tool_event(
            writer,
            SessionEventType.TOOL_VALIDATED,
            {
                "tool_call_id": call.tool_call_id,
                "name": call.name,
                "arguments": cast(dict[str, JSONValue], call.arguments),
                "risk": cast(dict[str, JSONValue], permission_request.risk.model_dump(mode="json")),
                "permission": _permission_payload(
                    permission_request,
                    decision,
                    mode=permission_mode,
                    matched_rule=(
                        "workspace-confinement"
                        if preflight_failure
                        else getattr(decision_metadata, "matched_rule", None)
                    ),
                    reason=(
                        preflight_failure.text
                        if preflight_failure
                        else getattr(decision_metadata, "reason", None)
                    ),
                    scope=(
                        "none" if preflight_failure else getattr(decision_metadata, "scope", "turn")
                    ),
                    timestamp=decision_at,
                ),
            },
            turn_id=turn_id,
            causation_id=causation_id,
            timestamp=decision_at,
        )
        _notify_lifecycle(
            on_lifecycle,
            SessionEventType.TOOL_VALIDATED.value,
            {
                "tool_call_id": call.tool_call_id,
                "name": call.name,
                "decision": decision.value,
                "resources": list(normalized_resources),
                "summary": permission_request.risk.summary,
                "reason": (
                    preflight_failure.text
                    if preflight_failure
                    else getattr(decision_metadata, "reason", "")
                ),
            },
        )
        if preflight_failure is not None:
            return (
                preflight_failure,
                validated_event.event_id if validated_event else causation_id,
            )
        if decision is PermissionDecision.CANCEL:
            return (
                ToolResult.failed(
                    call,
                    outcome=ToolOutcome.CANCELLED,
                    category="cancellation",
                    code="permission-cancelled",
                    message="Tool permission was cancelled by the user",
                ),
                validated_event.event_id if validated_event else causation_id,
            )
        if decision is not PermissionDecision.ALLOW:
            decision_reason = getattr(decision_metadata, "reason", None)
            non_interactive = (
                getattr(decision_metadata, "matched_rule", None) == "non-interactive-input"
            )
            return (
                ToolResult.failed(
                    call,
                    outcome=ToolOutcome.DENIED,
                    category="permission",
                    code="non-interactive-permission" if non_interactive else "read-only-policy",
                    message=(
                        decision_reason
                        if non_interactive and decision_reason
                        else "Tool operation was denied by the host Permission Policy"
                    ),
                ),
                validated_event.event_id if validated_event else causation_id,
            )
        started_event = self._append_tool_event(
            writer,
            SessionEventType.TOOL_STARTED,
            {
                "tool_call_id": call.tool_call_id,
                "name": call.name,
                "recovery": {
                    "tool_name": call.name,
                    "arguments": cast(dict[str, JSONValue], call.arguments),
                },
            },
            turn_id=turn_id,
            causation_id=validated_event.event_id if validated_event else causation_id,
        )
        _notify_lifecycle(
            on_lifecycle,
            SessionEventType.TOOL_STARTED.value,
            {
                "tool_call_id": call.tool_call_id,
                "name": call.name,
                "resources": list(normalized_resources),
            },
        )
        workspace.begin_tool_recovery(
            call.tool_call_id,
            call.name,
            {"arguments": cast(dict[str, JSONValue], call.arguments)},
        )
        try:
            result = await asyncio.wait_for(
                tool.execute(workspace, validated.arguments),
                timeout=tool.limits.timeout_seconds,
            )
            return result.for_call(call), started_event.event_id if started_event else causation_id
        except TimeoutError:
            return (
                ToolResult.failed(
                    call,
                    outcome=ToolOutcome.INTERRUPTED,
                    category="tool-timeout",
                    code="timeout",
                    message=(
                        "Tool execution exceeded its time limit; side effects could not be "
                        "proven to have stopped"
                    ),
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

    def _materialize_tool_result(
        self,
        result: ToolResult,
        writer: SessionWriter | None,
        *,
        configuration: EffectiveConfiguration | None,
        max_output_bytes: int,
        threshold: int,
        turn_id: str,
        causation_id: str | None,
    ) -> tuple[ToolResult, str | None]:
        """Redact a result and move its large serialized body behind an Artifact."""

        redacted = _redact_tool_result(result, configuration)
        content = redacted.text.encode("utf-8")
        if len(content) > max_output_bytes:
            bounded_preview = content[:ARTIFACT_PREVIEW_BYTES].decode("utf-8", errors="ignore")
            return (
                ToolResult.failed(
                    ToolCall(
                        tool_call_id=redacted.tool_call_id,
                        name=redacted.tool_name,
                        arguments={},
                    ),
                    category="tool-execution",
                    code="output-limit",
                    message="Tool output exceeded its absolute response limit",
                    data={
                        "output_bytes": len(content),
                        "preview": bounded_preview,
                        "truncated": True,
                    },
                ),
                causation_id,
            )
        if len(content) <= threshold or writer is None:
            return redacted, causation_id
        try:
            reference = writer.write_artifact(
                content,
                media_type=ARTIFACT_MEDIA_TYPE,
            )
            artifact_event = writer.append(
                SessionEventType.ARTIFACT_WRITTEN,
                {
                    "tool_call_id": redacted.tool_call_id,
                    "name": redacted.tool_name,
                    "artifact": cast(dict[str, JSONValue], reference.as_dict()),
                },
                turn_id=turn_id,
                causation_id=causation_id,
                timestamp=self._clock.now(),
            )
        except Exception:
            return (
                ToolResult.failed(
                    ToolCall(
                        tool_call_id=redacted.tool_call_id,
                        name=redacted.tool_name,
                        arguments={},
                    ),
                    category="persistence",
                    code="artifact-write-failed",
                    message="large Tool Result could not be durably stored as an Artifact",
                ),
                causation_id,
            )
        artifact_result = redacted.model_copy(update={"data": {"artifact": reference.as_dict()}})
        return artifact_result, artifact_event.event_id

    def _ensure_active_budget(self, started_at: datetime, max_active_seconds: int) -> None:
        elapsed = (self._clock.now() - started_at).total_seconds()
        if elapsed >= max_active_seconds:
            raise AgentLimitError("active execution budget exhausted")

    async def _wait_before_retry(self, failure: Failure, attempt: int) -> None:
        if failure.retry_after_seconds is not None:
            delay = min(failure.retry_after_seconds, 60.0)
        else:
            base = min(60.0, 0.25 * (2**attempt))
            delay = random.uniform(base * 0.5, base * 1.5)
        await self._retry_sleep(delay)

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
        artifact = result.data.get("artifact")
        if isinstance(artifact, dict):
            payload["artifact"] = cast(dict[str, JSONValue], artifact)
        if result.outcome is not ToolOutcome.SUCCESS and result.error is not None:
            failure = Failure(
                category=_tool_failure_category(result.error.category),
                code=result.error.code,
                source=result.tool_name,
                redacted_description=redact_secrets(result.error.message),
                retryable=False,
                required_user_action=(
                    "inspect the diagnostic error ID"
                    if result.outcome not in {ToolOutcome.DENIED, ToolOutcome.CANCELLED}
                    else "review the Tool decision or retry manually"
                ),
                details={"outcome": result.outcome.value},
                session_id=writer.session_id,
                turn_id=turn_id,
                tool_call_id=result.tool_call_id,
            )
            if self._diagnostic_logger is not None:
                failure = self._diagnostic_logger.record(failure)
            payload["failure"] = cast(dict[str, JSONValue], failure.as_dict())
        return writer.append(
            event_type,
            payload,
            turn_id=turn_id,
            causation_id=causation_id,
            timestamp=self._clock.now(),
        )

    def _append_plan_event(
        self,
        writer: SessionWriter | None,
        plan: PlanSnapshot,
        *,
        turn_id: str,
        causation_id: str | None,
    ) -> SessionEvent | None:
        if writer is None:
            return None
        return writer.append(
            SessionEventType.PLAN_UPDATED,
            {"plan": plan.as_dict()},
            turn_id=turn_id,
            causation_id=causation_id,
            timestamp=plan.updated_at,
        )

    def _record_failed_turn(
        self,
        writer: SessionWriter,
        turn_id: str,
        request_event: SessionEvent | None,
        failure: Failure,
    ) -> None:
        if (
            failure.category == "persistence"
            and failure.details.get("durability_uncertain") is True
        ):
            # The failed append may have reached storage but not become
            # durable.  A compensating terminal event would itself be unsafe.
            return
        if any(
            event.turn_id == turn_id
            and event.event_type in {SessionEventType.TURN_COMPLETED, SessionEventType.TURN_FAILED}
            for event in writer.events
        ):
            return
        try:
            causation_id = request_event.event_id if request_event is not None else None
            failure_payload = cast(dict[str, JSONValue], failure.as_dict())
            writer.append(
                SessionEventType.TURN_FAILED,
                {**failure_payload, "outcome": "failed"},
                turn_id=turn_id,
                causation_id=causation_id,
                timestamp=self._clock.now(),
            )
        except Exception as exc:
            if self._diagnostic_logger is not None:
                self._diagnostic_logger.record_exception(
                    exc,
                    session_id=writer.session_id,
                    turn_id=turn_id,
                    request_id=failure.request_id,
                    tool_call_id=failure.tool_call_id,
                )

    def _report_failure(
        self,
        exc: BaseException,
        *,
        session_id: str,
        turn_id: str,
        request_id: str | None = None,
        tool_call_id: str | None = None,
    ) -> Failure:
        failure = failure_from_exception(
            exc,
            session_id=session_id,
            turn_id=turn_id,
            request_id=request_id,
            tool_call_id=tool_call_id,
        )
        if self._diagnostic_logger is not None:
            failure = self._diagnostic_logger.record(failure)
        return failure


class AgentLimitError(RuntimeError):
    """Raised when the host safety budget prevents more model/tool work."""


class AgentTurnError(RuntimeError):
    """Raised when a Session already has an active Turn in this host."""


def _redact_tool_result(
    result: ToolResult,
    configuration: EffectiveConfiguration | None,
) -> ToolResult:
    secrets = _known_sensitive_environment_values(configuration)
    redacted_data, data_changed = _redact_value(result.data, None, secrets)
    redacted_error = result.error
    error_changed = False
    if result.error is not None:
        message = redact_secrets(result.error.message, secrets)
        error_changed = message != result.error.message
        if error_changed:
            redacted_error = ToolError(
                category=result.error.category,
                code=result.error.code,
                message=message,
            )
    if data_changed or error_changed:
        warnings.warn(
            "Tool output credential detection is best-effort; sensitive values were redacted",
            UserWarning,
            stacklevel=3,
        )
    if not isinstance(redacted_data, dict):
        raise TypeError("Tool Result data must remain an object after redaction")
    return result.model_copy(update={"data": redacted_data, "error": redacted_error})


def _known_sensitive_environment_values(
    configuration: EffectiveConfiguration | None,
) -> tuple[str, ...]:
    values: list[str] = []
    if configuration is not None and configuration.api_key:
        values.append(configuration.api_key)
    for key, value in os.environ.items():
        normalized = key.casefold().replace("-", "_")
        if _is_sensitive_key(normalized) and value:
            values.append(value)
    return tuple(dict.fromkeys(values))


def _redact_value(
    value: object,
    key: str | None,
    secrets: tuple[str, ...],
) -> tuple[object, bool]:
    if key is not None and _is_sensitive_key(key):
        return "<redacted>", value != "<redacted>"
    if isinstance(value, str):
        redacted = redact_secrets(value, secrets)
        return redacted, redacted != value
    if isinstance(value, dict):
        changed = False
        output: dict[str, Any] = {}
        for raw_key, raw_value in value.items():
            output_key = str(raw_key)
            output_value, item_changed = _redact_value(raw_value, output_key, secrets)
            output[output_key] = output_value
            changed = changed or item_changed
        return output, changed
    if isinstance(value, list):
        output_items: list[object] = []
        changed = False
        for item in value:
            output_item, item_changed = _redact_value(item, None, secrets)
            output_items.append(output_item)
            changed = changed or item_changed
        return output_items, changed
    return value, False


def _is_sensitive_key(key: str) -> bool:
    normalized = key.casefold().replace("-", "_")
    return any(
        marker in normalized
        for marker in (
            "api_key",
            "apikey",
            "access_token",
            "credential",
            "password",
            "secret",
            "token",
        )
    )


def _stream_output_bytes(event: StreamEvent) -> int:
    if isinstance(event, TextDelta):
        return len(event.text.encode("utf-8"))
    if isinstance(event, ToolCallArgumentDelta):
        return len(event.arguments.encode("utf-8"))
    if isinstance(event, ToolCallStarted):
        return len(event.name.encode("utf-8"))
    if isinstance(event, ToolCallCompleted) and event.arguments is not None:
        return len(json.dumps(event.arguments, ensure_ascii=False).encode("utf-8"))
    return 0


def _stream_has_model_output(events: list[StreamEvent]) -> bool:
    return any(
        isinstance(event, (TextDelta, ToolCallArgumentDelta, ToolCallStarted, ToolCallCompleted))
        for event in events
    )


def _is_safe_provider_retry(failure: Failure) -> bool:
    """Allow only transient, pre-output Provider failures to be retried."""

    if not failure.retryable or failure.category not in {
        "rate-limit",
        "network",
        "provider-timeout",
    }:
        return False
    return failure.details.get("automatic_retries_exhausted") is not True


def _tool_failure_category(category: str) -> str:
    if category in {"permission", "permission-denial"}:
        return "permission-denial"
    if category in {"timeout", "tool-timeout"}:
        return "tool-timeout"
    if category in {"tool-validation", "validation"}:
        return "tool-validation"
    if category in {"persistence"}:
        return "persistence"
    if category in {"cancellation"}:
        return "cancellation"
    return "tool-execution"


def _max_tool_output_bytes(
    registry: ToolRegistry,
    call: ToolCall,
    result: ToolResult,
) -> int:
    """Return the bound without re-resolving unknown Tool calls.

    Validation already performed the only meaningful registry lookup for an
    unknown call.  Looking up its name again while materializing the result
    would turn a recoverable model observation into an internal Turn failure.
    The global bound is sufficient for this bounded validation message; known
    Tool results still use the registered Tool's advertised limit.
    """

    if (
        result.outcome is ToolOutcome.INVALID
        and result.error is not None
        and result.error.category == "tool-validation"
        and result.error.code == "unknown-tool"
    ):
        return MAX_TOOL_RESPONSE_BYTES
    return min(registry.require(call.name).limits.max_output_bytes, MAX_TOOL_RESPONSE_BYTES)


_PLAN_TOOL_NAMES = {
    "inspect": frozenset({"read_file", "read_artifact", "search_files"}),
    "change": frozenset({"apply_patch", "create_file"}),
    "verify": frozenset({"shell"}),
}
_TOOL_PLAN_STEP = {
    tool_name: step_id
    for step_id, tool_names in _PLAN_TOOL_NAMES.items()
    for tool_name in tool_names
}


def _requires_plan(task: str, calls: tuple[object, ...]) -> bool:
    """Use a conservative observable heuristic for complex work.

    A single safe read remains lightweight.  Multiple requested operations or
    a task naming multiple phases (inspect/edit/test/verify) receives a Plan;
    the model never gets to decide whether host safety state is persisted.
    """

    if len(calls) > 1:
        return True
    lowered = task.casefold()
    phases = (
        ("inspect", "read", "search", "find"),
        ("edit", "change", "update", "modify", "create", "implement", "fix"),
        ("test", "verify", "check", "run"),
    )
    return sum(any(word in lowered for word in group) for group in phases) >= 2


def _new_plan(
    task: str,
    calls: tuple[object, ...],
    plan_id: str,
    timestamp: datetime,
) -> PlanSnapshot:
    names = {
        getattr(call, "name", "") for call in calls if isinstance(getattr(call, "name", ""), str)
    }
    descriptions: list[tuple[str, str]] = []
    if names & _PLAN_TOOL_NAMES["inspect"]:
        descriptions.append(("inspect", "Inspect the relevant repository code"))
    if names & _PLAN_TOOL_NAMES["change"] or any(
        word in task.casefold()
        for word in ("edit", "change", "update", "modify", "create", "implement", "fix")
    ):
        descriptions.append(("change", "Apply the requested code change"))
    if names & _PLAN_TOOL_NAMES["verify"] or any(
        word in task.casefold() for word in ("test", "verify", "check", "run")
    ):
        descriptions.append(("verify", "Run the requested verification"))
    if not descriptions:
        descriptions.append(("work", "Complete the requested repository work"))
    descriptions.append(("report", "Report the observable outcome"))
    steps = tuple(
        PlanStep(
            step_id=step_id,
            description=description,
            status=PlanStepStatus.IN_PROGRESS if index == 0 else PlanStepStatus.PENDING,
            updated_at=timestamp,
        )
        for index, (step_id, description) in enumerate(descriptions)
    )
    return PlanSnapshot(plan_id, task.strip(), steps, timestamp)


def _advance_plan(
    plan: PlanSnapshot,
    tool_name: str,
    result: ToolResult,
    timestamp: datetime,
) -> PlanSnapshot:
    category = _TOOL_PLAN_STEP.get(tool_name, "work")
    updated: list[PlanStep] = []
    matched = False
    for step in plan.steps:
        if step.step_id == category and step.status is not PlanStepStatus.COMPLETED:
            matched = True
            updated.append(
                replace(
                    step,
                    status=(
                        PlanStepStatus.COMPLETED
                        if result.outcome is ToolOutcome.SUCCESS
                        else PlanStepStatus.PENDING
                    ),
                    result_summary=(
                        "completed successfully"
                        if result.outcome is ToolOutcome.SUCCESS
                        else f"Tool observation: {result.outcome.value}"
                    ),
                    updated_at=timestamp,
                )
            )
        else:
            updated.append(step)
    if not matched:
        updated = list(plan.steps)
    if result.outcome is ToolOutcome.SUCCESS and not any(
        step.status is PlanStepStatus.IN_PROGRESS for step in updated
    ):
        for index, step in enumerate(updated):
            if step.status is PlanStepStatus.PENDING:
                updated[index] = replace(
                    step,
                    status=PlanStepStatus.IN_PROGRESS,
                    updated_at=timestamp,
                )
                break
    return PlanSnapshot(plan.plan_id, plan.objective, tuple(updated), timestamp)


def _finish_plan(
    plan: PlanSnapshot,
    report: CompletionReport,
    timestamp: datetime,
) -> PlanSnapshot:
    summary = "normal model stop"
    if report.unresolved_work:
        summary = "completed with unresolved Tool observations"
    return PlanSnapshot(
        plan.plan_id,
        plan.objective,
        tuple(
            replace(
                step,
                status=PlanStepStatus.COMPLETED,
                result_summary=step.result_summary or summary,
                updated_at=timestamp,
            )
            for step in plan.steps
        ),
        timestamp,
    )


def build_completion_report(
    observations: Iterable[tuple[ToolCall, ToolResult]],
) -> CompletionReport:
    """Build a factual report from the Tool observations of a Turn.

    Shell commands are verification evidence only after a successful Tool
    Result. Other outcomes remain unresolved observations, including an
    unsuccessful Shell attempt followed by a successful retry.
    """

    changed_files: set[str] = set()
    verification: list[str] = []
    unresolved: dict[str, str] = {}
    changed_tools = {"apply_patch", "create_file"}
    has_successful_verification = False
    for call, result in observations:
        raw_changed = result.data.get("changed_files", [])
        if call.name in changed_tools and isinstance(raw_changed, list):
            changed_files.update(item for item in raw_changed if isinstance(item, str))
        if call.name == "shell" and result.outcome is ToolOutcome.SUCCESS:
            command = _shell_command(call)
            if command is not None:
                verification.append(command)
                has_successful_verification = True
        observation_key = json.dumps(
            {"name": call.name, "arguments": call.arguments},
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        if result.outcome is ToolOutcome.SUCCESS:
            unresolved.pop(observation_key, None)
        else:
            code = result.error.code if result.error is not None else result.outcome.value
            if call.name == "shell":
                # Shell attempts are verification evidence only when the host
                # proves success. Keep each unsuccessful attempt separately so
                # a later retry cannot erase the failed/uncertain observation.
                command = _shell_command(call)
                description = (
                    f"Shell command {command!r} (Tool Call {call.tool_call_id})"
                    if command is not None
                    else f"Shell Tool Call {call.tool_call_id}"
                )
                unresolved[f"shell:{call.tool_call_id}"] = (
                    f"{description} ended with {result.outcome.value} ({code})"
                )
            else:
                unresolved[observation_key] = (
                    f"Tool Call {call.tool_call_id} ended with {result.outcome.value} ({code})"
                )
    if not verification:
        verification = ["unavailable"]
    unresolved_items = tuple(unresolved.values())
    if unresolved_items:
        outcome = "completed-with-unresolved-work"
        if not has_successful_verification:
            next_action = (
                "Verification is unavailable; review the reported Tool observations and "
                "safely rerun the relevant verification command."
            )
        else:
            next_action = "Resolve the reported Tool observations and rerun verification."
    elif not has_successful_verification and changed_files:
        outcome = "completed"
        next_action = "Run the relevant verification command before delivery."
    else:
        outcome = "completed"
        next_action = "No further action is required."
    return CompletionReport(
        outcome=outcome,
        verification=tuple(verification),
        changed_files=tuple(sorted(changed_files)),
        unresolved_work=unresolved_items,
        next_action=next_action,
    )


def _shell_command(call: ToolCall) -> str | None:
    command = call.arguments.get("command")
    if isinstance(command, str) and command.strip():
        return command.strip()
    return None


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
    request: PermissionRequest,
    decision: PermissionDecision,
    *,
    mode: str,
    matched_rule: str | None,
    reason: str | None,
    scope: str,
    timestamp: datetime,
) -> dict[str, JSONValue]:
    allowed = decision is PermissionDecision.ALLOW
    return {
        "tool_call_id": request.call.tool_call_id,
        "risk": cast(dict[str, JSONValue], request.risk.model_dump(mode="json")),
        "mode": mode,
        "decision": decision.value,
        "matched_rule": matched_rule or ("safe-read" if allowed else "read-only-deny"),
        "reason": reason
        or (
            "safe Workspace read automatically authorized"
            if allowed
            else "non-read Tool Call denied by the read-only host policy"
        ),
        "scope": scope,
        "resource_summary": list(request.risk.resources),
        "argument_hash": request.call.argument_hash,
        "timestamp": timestamp.isoformat(),
    }


def _argument_hash(call: ToolCall) -> str:
    arguments = json.dumps(
        {"name": call.name, "arguments": call.arguments},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(arguments).hexdigest()


def _workspace_failure(call: ToolCall, exc: WorkspacePathError) -> ToolResult:
    return ToolResult.failed(
        call,
        outcome=ToolOutcome.DENIED if exc.hard_denial else ToolOutcome.FAILED,
        category="permission" if exc.hard_denial else "tool-execution",
        code=exc.code,
        message=str(exc),
    )


def _preflight_failure(
    call: ToolCall, exc: WorkspacePathError | WriteValidationError
) -> ToolResult:
    if isinstance(exc, WorkspacePathError):
        return _workspace_failure(call, exc)
    return ToolResult.failed(
        call,
        outcome=ToolOutcome.FAILED,
        category="tool-validation",
        code=exc.code,
        message=str(exc),
    )


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
    summary: dict[str, object] = {
        "sequence": event.sequence,
        "event_id": event.event_id,
    }
    summary.update(
        {
            key: value
            for key, value in event.payload.items()
            if key in {"tool_call_id", "name", "outcome", "result_text", "arguments"}
        }
    )
    return summary


def _selected_context_events(
    writer: SessionWriter | None, summary_boundary: int
) -> tuple[dict[str, object], ...]:
    if writer is None:
        return ()
    return tuple(
        {"type": event.event_type, **_tool_event_summary(event)}
        for event in writer.events
        if event.event_type.startswith("tool.") and event.sequence > summary_boundary
    )


def _session_message_sources(
    writer: SessionWriter | None,
    summary_boundary: int,
    history: Sequence[Message],
    *,
    current_user_event: SessionEvent | None,
    summary: ContextSummary | None,
    plan: PlanSnapshot | None,
) -> tuple[dict[str, object], ...]:
    """Map model-visible Session data to exact non-secret Session Event identities."""

    if writer is None:
        return ()
    sources = _session_state_sources(writer, summary=summary, plan=plan)
    candidates = [
        event
        for event in writer.events
        if event.sequence > summary_boundary
        and event.event_type
        in {
            SessionEventType.USER_MESSAGE,
            SessionEventType.ASSISTANT_MESSAGE,
            SessionEventType.TOOL_COMPLETED,
            SessionEventType.TOOL_FAILED,
            SessionEventType.TOOL_INTERRUPTED,
        }
    ]
    cursor = 0
    for message in history:
        for candidate_index in range(cursor, len(candidates)):
            event = candidates[candidate_index]
            if not _event_matches_message(event, message):
                continue
            sources.append(
                {
                    "source_kind": "session-event",
                    "event_id": event.event_id,
                    "sequence": event.sequence,
                    "event_type": event.event_type,
                    "projection": _message_projection(message),
                }
            )
            cursor = candidate_index + 1
            break
    if current_user_event is not None:
        sources.append(_context_source(current_user_event, projection="current-user-message"))
    return tuple(sources)


def _session_state_sources(
    writer: SessionWriter,
    *,
    summary: ContextSummary | None,
    plan: PlanSnapshot | None,
) -> list[dict[str, object]]:
    """Identify durable source events for visible derived Session state."""

    sources: list[dict[str, object]] = []
    if summary is not None:
        event = _latest_state_event(
            writer.events,
            SessionEventType.CONTEXT_COMPACTED,
        )
        if event is not None:
            sources.append(_context_source(event, projection="context-summary"))
    if plan is not None:
        event = _latest_state_event(
            writer.events,
            SessionEventType.PLAN_UPDATED,
        )
        if event is not None:
            sources.append(_context_source(event, projection="plan-snapshot"))
    return sources


def _latest_state_event(
    events: Sequence[SessionEvent],
    event_type: SessionEventType,
) -> SessionEvent | None:
    for event in reversed(events):
        if event.event_type is event_type:
            return event
    return None


def _context_source(event: SessionEvent, *, projection: str) -> dict[str, object]:
    return {
        "source_kind": "session-event",
        "event_id": event.event_id,
        "sequence": event.sequence,
        "event_type": event.event_type,
        "projection": projection,
    }


def _event_matches_message(event: SessionEvent, message: Message) -> bool:
    if isinstance(message, UserMessage):
        return (
            event.event_type == SessionEventType.USER_MESSAGE
            and event.payload.get("role") == "user"
            and event.payload.get("content") == message.content
        )
    if isinstance(message, AssistantMessage):
        return (
            event.event_type == SessionEventType.ASSISTANT_MESSAGE
            and event.payload == _assistant_payload(message)
        )
    if isinstance(message, ToolResultMessage):
        return (
            event.event_type
            in {
                SessionEventType.TOOL_COMPLETED,
                SessionEventType.TOOL_FAILED,
                SessionEventType.TOOL_INTERRUPTED,
            }
            and event.payload.get("tool_call_id") == message.tool_call_id
            and event.payload.get("result_text") == message.content
            and event.payload.get("outcome") == message.outcome
        )
    return False


def _message_projection(message: Message) -> str:
    if isinstance(message, UserMessage):
        return "user-message"
    if isinstance(message, AssistantMessage):
        return "assistant-message"
    return "tool-result-message"


def _event_request_id(event: SessionEvent | None) -> str | None:
    if event is None:
        return None
    value = event.payload.get("request_id")
    return value if isinstance(value, str) else None


def _notify_lifecycle(
    observer: LifecycleObserver | None,
    event_type: str,
    payload: Mapping[str, JSONValue],
) -> None:
    """Notify presentation code without making it part of the safety boundary."""

    if observer is None:
        return
    try:
        observer(event_type, payload)
    except Exception:
        # A broken terminal, logger, or test observer must not change durable
        # Agent semantics or turn a successful operation into a failure.
        return
