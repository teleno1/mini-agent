"""The first application use case: run one text-only Turn."""

from __future__ import annotations

import asyncio
import inspect
import random
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import cast

from mini_agent.application.ports import (
    Clock,
    EventObserver,
    IDGenerator,
    ModelProvider,
    ResumedSession,
    SessionStore,
    SessionWriter,
)
from mini_agent.application.ports import (
    ContextBuilder as ContextBuilderPort,
)
from mini_agent.configuration import ConfigurationResolver, EffectiveConfiguration
from mini_agent.context import ContextFrame
from mini_agent.diagnostics import DiagnosticLogger, failure_from_exception
from mini_agent.domain.messages import AssistantMessage, Message, UserMessage
from mini_agent.domain.sessions import JSONValue, SessionEvent, SessionEventType
from mini_agent.domain.streams import Failure, StreamEvent, TextDelta
from mini_agent.domain.turns import close_text_response


@dataclass(frozen=True, slots=True)
class TurnResult:
    """Observable result of a successfully closed text-only Turn."""

    session_id: str
    turn_id: str
    user_message: UserMessage
    assistant_message: AssistantMessage
    started_at: datetime
    completed_at: datetime
    usage_input_tokens: int
    usage_output_tokens: int
    stream_events: tuple[StreamEvent, ...]


class TextTurnApplication:
    """Orchestrate a single provider request through explicit Application Ports."""

    def __init__(
        self,
        provider: ModelProvider,
        clock: Clock,
        id_generator: IDGenerator,
        session_store: SessionStore | None = None,
        context_builder: ContextBuilderPort | None = None,
        configuration: EffectiveConfiguration | None = None,
        configuration_resolver: ConfigurationResolver | None = None,
        request_targets: tuple[str, ...] = (),
        diagnostic_logger: DiagnosticLogger | None = None,
        max_retries: int = 2,
        retry_sleep: Callable[[float], Awaitable[object]] = asyncio.sleep,
    ) -> None:
        self._provider = provider
        self._clock = clock
        self._id_generator = id_generator
        self._session_store = session_store
        self._context_builder = context_builder
        self._configuration = configuration
        self._configuration_resolver = configuration_resolver
        self._request_targets = request_targets
        diagnostic_root = getattr(session_store, "workspace_root", Path.cwd())
        self._diagnostic_logger = diagnostic_logger or DiagnosticLogger(
            Path(diagnostic_root),
            id_generator=id_generator,
        )
        if max_retries < 0 or max_retries > 2:
            raise ValueError("text Turn retries must be between zero and two")
        self._max_retries = max_retries
        self._retry_sleep = retry_sleep

    async def run(
        self,
        task: str,
        on_event: EventObserver | None = None,
        *,
        session_id: str | None = None,
    ) -> TurnResult:
        user_message = UserMessage(task)
        requested_session_id = session_id
        if requested_session_id is not None and self._session_store is None:
            raise ValueError("session_id requires a Session Store")
        session_id = session_id or self._id_generator.new_id("session")
        turn_id = self._id_generator.new_id("turn")
        started_at = self._clock.now()
        events: list[StreamEvent] = []
        writer: SessionWriter | None = None
        turn_started: SessionEvent | None = None
        request_started: SessionEvent | None = None
        request_completed = False
        history: tuple[Message, ...] = ()
        frame: ContextFrame | None = None
        resumed_session: ResumedSession | None = None
        effective_configuration = self._configuration

        try:
            if self._session_store is not None:
                if requested_session_id is not None:
                    resumed_session = self._session_store.resume(session_id)
                    history = resumed_session.messages
                    writer = self._session_store.open_writer(session_id)
                else:
                    writer = self._session_store.create(session_id, created_at=started_at)
                if self._configuration_resolver is not None:
                    effective_configuration = self._configuration_resolver.resolve(
                        session_overrides=(
                            resumed_session.configuration_overrides
                            if resumed_session is not None
                            else None
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
                request_id = self._id_generator.new_id("request")
                frame = self._build_context_frame(
                    task,
                    request_id=request_id,
                    session_id=session_id,
                    history=history,
                    configuration=effective_configuration,
                    included_event_range=(1, len(writer.events)),
                )
                if frame is not None:
                    previous_hashes = _previous_instruction_hashes(resumed_session)
                    if (
                        resumed_session is not None
                        and previous_hashes != frame.manifest.instruction_hashes
                    ):
                        writer.append(
                            SessionEventType.INSTRUCTION_CHANGED,
                            {
                                "previous_hashes": [
                                    {"path": path, "sha256": sha256}
                                    for path, sha256 in previous_hashes
                                ],
                                "current_hashes": [
                                    {"path": path, "sha256": sha256}
                                    for path, sha256 in frame.manifest.instruction_hashes
                                ],
                            },
                            turn_id=turn_id,
                            causation_id=user_event.event_id,
                            timestamp=self._clock.now(),
                        )
                    writer.append(
                        SessionEventType.CONTEXT_MANIFEST_RECORDED,
                        {
                            "manifest": frame.manifest.as_dict(),
                            "manifest_hash": frame.manifest.manifest_hash_without_self(),
                        },
                        turn_id=turn_id,
                        causation_id=user_event.event_id,
                        timestamp=self._clock.now(),
                    )
                request_started = writer.append(
                    SessionEventType.MODEL_REQUEST_STARTED,
                    {
                        "request_id": request_id,
                        "message_count": len(history) + 1,
                    },
                    turn_id=turn_id,
                    causation_id=user_event.event_id,
                    timestamp=self._clock.now(),
                )
        except BaseException as exc:
            failure = self._report_failure(exc, session_id=session_id, turn_id=turn_id)
            if writer is not None:
                self._record_failed_turn(
                    writer,
                    turn_id=turn_id,
                    request_started=request_started,
                    request_completed=request_completed,
                    failure=failure,
                )
                writer.close()
            raise

        if frame is None and self._context_builder is not None:
            frame = self._build_context_frame(
                task,
                request_id=self._id_generator.new_id("request"),
                session_id=session_id,
                history=history,
                configuration=effective_configuration,
            )

        request_messages: tuple[Message, ...] | ContextFrame
        if frame is not None:
            request_messages = frame
        else:
            request_messages = (*history, user_message)

        retry_count = 0
        response = None
        try:
            while True:
                stream_events: list[StreamEvent] = []
                try:
                    async for event in self._provider.stream(request_messages):
                        stream_events.append(event)
                        if on_event is not None:
                            try:
                                observed = on_event(event)
                                if inspect.isawaitable(observed):
                                    await observed
                            except Exception:
                                # A broken stdout/renderer must not turn a valid model
                                # response into a false Provider failure.
                                pass
                    response = close_text_response(tuple(stream_events))
                    events.extend(stream_events)
                    break
                except BaseException as exc:
                    failure = self._report_failure(
                        exc,
                        session_id=session_id,
                        turn_id=turn_id,
                        request_id=_event_request_id(request_started),
                    )
                    if writer is not None and request_started is not None:
                        failure_payload = cast(dict[str, JSONValue], failure.as_dict())
                        writer.append(
                            SessionEventType.MODEL_REQUEST_FAILED,
                            {
                                **failure_payload,
                                "request_id": request_started.payload["request_id"],
                            },
                            turn_id=turn_id,
                            causation_id=request_started.event_id,
                            timestamp=self._clock.now(),
                        )
                        request_started = None
                    if (
                        _safe_text_retry(failure)
                        and not _stream_has_output(stream_events)
                        and retry_count < self._max_retries
                    ):
                        await self._wait_before_retry(failure, retry_count)
                        retry_count += 1
                        if writer is not None:
                            retry_request_id = self._id_generator.new_id("request")
                            request_started = writer.append(
                                SessionEventType.MODEL_REQUEST_STARTED,
                                {
                                    "request_id": retry_request_id,
                                    "message_count": len(history) + 1,
                                },
                                turn_id=turn_id,
                                causation_id=turn_started.event_id if turn_started else None,
                                timestamp=self._clock.now(),
                            )
                        continue
                    raise

            if response is None:
                raise RuntimeError("Provider returned no response")
            completed_at = self._clock.now()
            if writer is not None and request_started is not None:
                completed_event = writer.append(
                    SessionEventType.MODEL_REQUEST_COMPLETED,
                    {
                        "request_id": request_started.payload["request_id"],
                        "input_tokens": response.usage.input_tokens,
                        "output_tokens": response.usage.output_tokens,
                    },
                    turn_id=turn_id,
                    causation_id=request_started.event_id,
                    timestamp=completed_at,
                )
                request_completed = True
                assistant_event = writer.append(
                    SessionEventType.ASSISTANT_MESSAGE,
                    {"role": "assistant", "content": response.message.content},
                    turn_id=turn_id,
                    causation_id=completed_event.event_id,
                    timestamp=completed_at,
                )
                writer.append(
                    SessionEventType.TURN_COMPLETED,
                    {
                        "outcome": "completed",
                        "input_tokens": response.usage.input_tokens,
                        "output_tokens": response.usage.output_tokens,
                    },
                    turn_id=turn_id,
                    causation_id=assistant_event.event_id,
                    timestamp=completed_at,
                )
            return TurnResult(
                session_id=session_id,
                turn_id=turn_id,
                user_message=user_message,
                assistant_message=response.message,
                started_at=started_at,
                completed_at=completed_at,
                usage_input_tokens=response.usage.input_tokens,
                usage_output_tokens=response.usage.output_tokens,
                stream_events=tuple(events),
            )
        except BaseException as exc:
            failure = self._report_failure(
                exc,
                session_id=session_id,
                turn_id=turn_id,
                request_id=_event_request_id(request_started),
            )
            if writer is not None and turn_started is not None:
                self._record_failed_turn(
                    writer,
                    turn_id=turn_id,
                    request_started=request_started,
                    request_completed=request_completed,
                    failure=failure,
                )
            raise
        finally:
            if writer is not None:
                writer.close()

    async def _wait_before_retry(self, failure: Failure, attempt: int) -> None:
        if failure.retry_after_seconds is not None:
            delay = min(failure.retry_after_seconds, 60.0)
        else:
            base = min(60.0, 0.25 * (2**attempt))
            delay = random.uniform(base * 0.5, base * 1.5)
        await self._retry_sleep(delay)

    def _build_context_frame(
        self,
        task: str,
        *,
        request_id: str,
        session_id: str,
        history: tuple[Message, ...],
        configuration: EffectiveConfiguration | None = None,
        included_event_range: tuple[int, int] | None = None,
    ) -> ContextFrame | None:
        if self._context_builder is None:
            return None
        return self._context_builder.build(
            task,
            request_id=request_id,
            session_id=session_id,
            targets=self._request_targets,
            history=history,
            configuration=configuration,
            included_event_range=included_event_range,
        )

    def _record_failed_turn(
        self,
        writer: SessionWriter,
        *,
        turn_id: str,
        request_started: SessionEvent | None,
        request_completed: bool,
        failure: Failure,
    ) -> None:
        # Session persistence itself is the safety boundary.  If it is already
        # broken, the original exception remains the one reported to callers.
        if (
            failure.category == "persistence"
            and failure.details.get("durability_uncertain") is True
        ):
            return
        timestamp = self._clock.now()
        try:
            if any(
                event.turn_id == turn_id
                and event.event_type
                in {SessionEventType.TURN_COMPLETED, SessionEventType.TURN_FAILED}
                for event in writer.events
            ):
                return
            causation_id = request_started.event_id if request_started is not None else None
            payload = cast(dict[str, JSONValue], failure.as_dict())
            if request_started is not None and not request_completed:
                failed_request = writer.append(
                    SessionEventType.MODEL_REQUEST_FAILED,
                    {
                        **payload,
                        "request_id": request_started.payload["request_id"],
                    },
                    turn_id=turn_id,
                    causation_id=causation_id,
                    timestamp=timestamp,
                )
                causation_id = failed_request.event_id
            writer.append(
                SessionEventType.TURN_FAILED,
                {**payload, "outcome": "failed"},
                turn_id=turn_id,
                causation_id=causation_id,
                timestamp=timestamp,
            )
        except Exception as exc:
            if self._diagnostic_logger is not None:
                self._diagnostic_logger.record_exception(
                    exc,
                    session_id=writer.session_id,
                    turn_id=turn_id,
                )

    def _report_failure(
        self,
        exc: BaseException,
        *,
        session_id: str,
        turn_id: str,
        request_id: str | None = None,
    ) -> Failure:
        failure = failure_from_exception(
            exc,
            session_id=session_id,
            turn_id=turn_id,
            request_id=request_id,
        )
        if self._diagnostic_logger is not None:
            failure = self._diagnostic_logger.record(failure)
        return failure


def _safe_text_retry(failure: Failure) -> bool:
    return (
        failure.retryable
        and failure.details.get("automatic_retries_exhausted") is not True
        and failure.category
        in {
            "rate-limit",
            "network",
            "provider-timeout",
        }
    )


def _stream_has_output(events: list[StreamEvent]) -> bool:
    return any(isinstance(event, TextDelta) for event in events)


def _event_request_id(event: SessionEvent | None) -> str | None:
    if event is None:
        return None
    value = event.payload.get("request_id")
    return value if isinstance(value, str) else None


def _previous_instruction_hashes(
    resumed_session: ResumedSession | None,
) -> tuple[tuple[str, str], ...]:
    if resumed_session is None or not resumed_session.context_manifests:
        return ()
    raw_hashes = resumed_session.context_manifests[-1].get("instruction_hashes", [])
    if not isinstance(raw_hashes, list):
        return ()
    hashes: list[tuple[str, str]] = []
    for item in raw_hashes:
        if not isinstance(item, dict):
            continue
        path = item.get("path")
        sha256 = item.get("sha256")
        if isinstance(path, str) and isinstance(sha256, str):
            hashes.append((path, sha256))
    return tuple(hashes)
