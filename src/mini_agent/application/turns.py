"""The first application use case: run one text-only Turn."""

from __future__ import annotations

import inspect
from dataclasses import dataclass
from datetime import datetime

from mini_agent.application.ports import (
    Clock,
    EventObserver,
    IDGenerator,
    ModelProvider,
    SessionStore,
    SessionWriter,
)
from mini_agent.application.ports import (
    ContextBuilder as ContextBuilderPort,
)
from mini_agent.configuration import EffectiveConfiguration
from mini_agent.context import ContextFrame
from mini_agent.domain.messages import AssistantMessage, Message, UserMessage
from mini_agent.domain.sessions import SessionEvent, SessionEventType
from mini_agent.domain.streams import Failure, StreamEvent
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
        request_targets: tuple[str, ...] = (),
    ) -> None:
        self._provider = provider
        self._clock = clock
        self._id_generator = id_generator
        self._session_store = session_store
        self._context_builder = context_builder
        self._configuration = configuration
        self._request_targets = request_targets

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

        try:
            if self._session_store is not None:
                if requested_session_id is not None:
                    resumed = self._session_store.resume(session_id)
                    history = resumed.messages
                    writer = self._session_store.open_writer(session_id)
                else:
                    writer = self._session_store.create(session_id, created_at=started_at)
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
                )
                if frame is not None:
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
            if writer is not None:
                self._record_failed_turn(
                    writer,
                    turn_id=turn_id,
                    request_started=request_started,
                    request_completed=request_completed,
                    cause=_failure_payload(exc),
                )
                writer.close()
            raise

        if frame is None and self._context_builder is not None:
            frame = self._build_context_frame(
                task,
                request_id=self._id_generator.new_id("request"),
                session_id=session_id,
                history=history,
            )

        request_messages: tuple[Message, ...] | ContextFrame
        if frame is not None:
            request_messages = frame
        else:
            request_messages = (*history, user_message)

        try:
            async for event in self._provider.stream(request_messages):
                events.append(event)
                if on_event is not None:
                    observed = on_event(event)
                    if inspect.isawaitable(observed):
                        await observed

            response = close_text_response(tuple(events))
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
            if writer is not None and turn_started is not None:
                self._record_failed_turn(
                    writer,
                    turn_id=turn_id,
                    request_started=request_started,
                    request_completed=request_completed,
                    cause=_failure_payload(exc),
                )
            raise
        finally:
            if writer is not None:
                writer.close()

    def _build_context_frame(
        self,
        task: str,
        *,
        request_id: str,
        session_id: str,
        history: tuple[Message, ...],
    ) -> ContextFrame | None:
        if self._context_builder is None:
            return None
        return self._context_builder.build(
            task,
            request_id=request_id,
            session_id=session_id,
            targets=self._request_targets,
            history=history,
        )

    def _record_failed_turn(
        self,
        writer: SessionWriter,
        *,
        turn_id: str,
        request_started: SessionEvent | None,
        request_completed: bool,
        cause: dict[str, str],
    ) -> None:
        # Session persistence itself is the safety boundary.  If it is already
        # broken, the original exception remains the one reported to callers.
        timestamp = self._clock.now()
        try:
            causation_id = request_started.event_id if request_started is not None else None
            if request_started is not None and not request_completed:
                failed_request = writer.append(
                    SessionEventType.MODEL_REQUEST_FAILED,
                    {**cause, "request_id": request_started.payload["request_id"]},
                    turn_id=turn_id,
                    causation_id=causation_id,
                    timestamp=timestamp,
                )
                causation_id = failed_request.event_id
            writer.append(
                SessionEventType.TURN_FAILED,
                {**cause, "outcome": "failed"},
                turn_id=turn_id,
                causation_id=causation_id,
                timestamp=timestamp,
            )
        except Exception:
            return


def _failure_payload(exc: BaseException) -> dict[str, str]:
    if hasattr(exc, "event"):
        event = getattr(exc, "event")
        failure = getattr(event, "failure", None)
        if isinstance(failure, Failure):
            return {
                "category": failure.category,
                "source": failure.source,
                "description": failure.redacted_description,
            }
    return {
        "category": "provider",
        "source": "application",
        "description": f"{type(exc).__name__}: {str(exc)[:200]}",
    }
