"""Direct async OpenAI-compatible Chat Completions Provider adapter.

The adapter intentionally speaks only the small streaming subset used by Mini
Agent.  Provider-specific JSON is consumed here and never crosses the
Application Port boundary.
"""

from __future__ import annotations

import asyncio
import json
import random
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass, replace
from types import TracebackType
from typing import Any, Self, cast
from urllib.parse import urlsplit

import httpx

from mini_agent.adapters.ids import UUIDIdGenerator
from mini_agent.application.ports import IDGenerator
from mini_agent.configuration import EffectiveConfiguration, redact_secrets
from mini_agent.context import ContextFrame, ContextMessage
from mini_agent.domain.messages import AssistantMessage, Message, ToolResultMessage, UserMessage
from mini_agent.domain.streams import (
    Failure,
    ResponseCompleted,
    ResponseFailed,
    ResponseStarted,
    StreamEvent,
    TextDelta,
    ToolCallArgumentDelta,
    ToolCallCompleted,
    ToolCallStarted,
    UsageReported,
)
from mini_agent.tools.contracts import ToolDefinition


@dataclass(frozen=True, slots=True)
class ProviderTimeouts:
    """Independent bounds for one Provider request."""

    connect: float = 10.0
    first_event: float = 60.0
    idle: float = 60.0
    total: float = 10 * 60.0

    def __post_init__(self) -> None:
        if any(value <= 0 for value in (self.connect, self.first_event, self.idle, self.total)):
            raise ValueError("Provider timeouts must be positive")

    @property
    def connect_seconds(self) -> float:
        return self.connect

    @property
    def first_event_seconds(self) -> float:
        return self.first_event

    @property
    def idle_seconds(self) -> float:
        return self.idle

    @property
    def total_seconds(self) -> float:
        return self.total


@dataclass(frozen=True, slots=True)
class ProviderCapabilities:
    """Capabilities known at the Provider boundary.

    Chat Completions has no portable capability-discovery endpoint.  System
    roles are part of this supported subset; structured-tool support is
    observed from successful tool-call chunks and is marked unavailable when
    a Provider explicitly rejects the ``tools`` request field.
    """

    system_messages: bool = True
    structured_tool_calls: bool | None = None

    @property
    def structured_tools(self) -> bool | None:
        return self.structured_tool_calls


class ProviderConfigurationError(ValueError):
    """Raised when an adapter cannot form a safe Chat Completions request."""


class _ProviderFailure(RuntimeError):
    def __init__(self, failure: Failure, *, retry_after: float | None = None) -> None:
        super().__init__(failure.redacted_description)
        self.failure = failure
        self.retry_after = retry_after


class _ProviderTimeout(_ProviderFailure):
    pass


class _ProtocolViolation(_ProviderFailure):
    pass


@dataclass(slots=True)
class _ToolAccumulator:
    index: int
    tool_call_id: str | None = None
    name: str | None = None
    argument_fragments: list[str] | None = None
    started: bool = False
    emitted_fragments: int = 0

    def __post_init__(self) -> None:
        if self.argument_fragments is None:
            self.argument_fragments = []

    @property
    def arguments(self) -> str:
        return "".join(self.argument_fragments or ())


class _ResponseState:
    def __init__(self, fallback_request_id: str) -> None:
        self.request_id = fallback_request_id
        self.started = False
        self.role_seen = False
        self.finish_reason: str | None = None
        self.usage_seen = False
        self.usage: tuple[int, int] | None = None
        self.tool_calls: dict[int, _ToolAccumulator] = {}


Sleep = Callable[[float], Awaitable[object]]


class OpenAICompatibleModelProvider:
    """Stream the documented OpenAI-compatible Chat Completions subset."""

    def __init__(
        self,
        base_url: str | EffectiveConfiguration | None = None,
        api_key: str | None = None,
        model: str | None = None,
        *,
        configuration: EffectiveConfiguration | None = None,
        timeouts: ProviderTimeouts | None = None,
        client: httpx.AsyncClient | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        id_generator: IDGenerator | None = None,
        tool_definitions: Sequence[Mapping[str, object] | ToolDefinition] = (),
        tools: Sequence[Mapping[str, object] | ToolDefinition] | None = None,
        max_retries: int = 2,
        backoff_base_seconds: float = 0.25,
        sleep: Sleep = asyncio.sleep,
    ) -> None:
        if isinstance(base_url, EffectiveConfiguration):
            if configuration is not None:
                raise ProviderConfigurationError("provide configuration only once")
            configuration = base_url
            base_url = None
        if configuration is not None:
            if base_url is None:
                base_url = configuration.provider_base_url
            if api_key is None:
                api_key = configuration.api_key
            if model is None:
                model = configuration.model
        if base_url is None or model is None:
            raise ProviderConfigurationError("Provider Base URL and model are required")
        self.base_url = _validated_base_url(base_url)
        if not model.strip():
            raise ProviderConfigurationError("Provider model cannot be blank")
        if api_key is not None and not api_key.strip():
            raise ProviderConfigurationError("Provider API key cannot be blank")
        if max_retries < 0 or max_retries > 2:
            raise ProviderConfigurationError("Provider retries must be between 0 and 2")
        if backoff_base_seconds < 0:
            raise ProviderConfigurationError("Provider retry backoff cannot be negative")

        self.model = model.strip()
        self._api_key = api_key
        self.timeouts = timeouts or ProviderTimeouts()
        self.max_retries = max_retries
        self.backoff_base_seconds = backoff_base_seconds
        self._sleep = sleep
        self._id_generator = id_generator or UUIDIdGenerator()
        if tools is not None:
            if tool_definitions:
                raise ProviderConfigurationError("provide tool definitions only once")
            tool_definitions = tools
        self._tool_definitions = tuple(tool_definitions)
        self._structured_tools_supported: bool | None = None
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(
            transport=transport,
            timeout=httpx.Timeout(
                connect=self.timeouts.connect,
                read=self.timeouts.idle,
                write=self.timeouts.total,
                pool=self.timeouts.connect,
            ),
        )

    @classmethod
    def from_configuration(
        cls,
        configuration: EffectiveConfiguration,
        **kwargs: Any,
    ) -> OpenAICompatibleModelProvider:
        """Construct an adapter without exposing the API key in a call site."""

        return cls(configuration=configuration, **kwargs)

    @property
    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(structured_tool_calls=self._structured_tools_supported)

    @property
    def supports_structured_tools(self) -> bool | None:
        """Return the latest request-level structured-tool capability signal."""

        return self._structured_tools_supported

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        del exc_type, exc_value, traceback
        await self.aclose()

    def stream(self, messages: Sequence[Message] | ContextFrame) -> AsyncIterator[StreamEvent]:
        return self._stream(messages)

    async def _stream(
        self, messages: Sequence[Message] | ContextFrame
    ) -> AsyncIterator[StreamEvent]:
        payload, fallback_request_id = _request_payload(
            messages,
            model=self.model,
            static_tool_definitions=self._tool_definitions,
            fallback_request_id=self._id_generator.new_id("request"),
        )
        attempt = 0
        attempt_request_id = fallback_request_id
        attempt_request_ids = [attempt_request_id]
        while True:
            started = False
            try:
                deadline = time.monotonic() + self.timeouts.total
                async with self._response_stream(payload) as response:
                    if response.status_code < 200 or response.status_code >= 300:
                        raise await self._http_failure(response)
                    state = _ResponseState(attempt_request_id)
                    async for data in _iter_sse_data(response, self.timeouts, deadline=deadline):
                        if data == "[DONE]":
                            for event in _finish_response(state):
                                if isinstance(event, ResponseStarted):
                                    started = True
                                yield event
                            return
                        raw = _decode_chunk(data)
                        for event in self._events_for_chunk(raw, state):
                            if isinstance(event, ResponseStarted):
                                started = True
                            yield event
            except asyncio.CancelledError:
                raise
            except _ProviderFailure as exc:
                if not started and exc.failure.retryable and attempt < self.max_retries:
                    await self._wait_before_retry(attempt, exc.retry_after)
                    attempt += 1
                    attempt_request_id = self._id_generator.new_id("request")
                    attempt_request_ids.append(attempt_request_id)
                    continue
                yield ResponseFailed(
                    _provider_attempt_failure(
                        exc.failure,
                        attempt,
                        self.max_retries,
                        started,
                        attempt_request_ids,
                    )
                )
                return
            except TimeoutError:
                failure = _stream_failure(
                    "total-timeout",
                    "the Provider request exceeded its total timeout",
                    retryable=not started,
                )
                if not started and attempt < self.max_retries:
                    await self._wait_before_retry(attempt, None)
                    attempt += 1
                    attempt_request_id = self._id_generator.new_id("request")
                    attempt_request_ids.append(attempt_request_id)
                    continue
                yield ResponseFailed(
                    _provider_attempt_failure(
                        failure,
                        attempt,
                        self.max_retries,
                        started,
                        attempt_request_ids,
                    )
                )
                return
            except httpx.TimeoutException as exc:
                failure = _failure(
                    category="provider-timeout",
                    code="transport-timeout",
                    description="the Provider request timed out",
                    retryable=not started,
                    action="retry" if not started else "retry manually from the last durable frame",
                    cause=type(exc).__name__,
                )
                if not started and attempt < self.max_retries:
                    await self._wait_before_retry(attempt, None)
                    attempt += 1
                    attempt_request_id = self._id_generator.new_id("request")
                    attempt_request_ids.append(attempt_request_id)
                    continue
                yield ResponseFailed(
                    _provider_attempt_failure(
                        failure,
                        attempt,
                        self.max_retries,
                        started,
                        attempt_request_ids,
                    )
                )
                return

            except httpx.HTTPError as exc:
                failure = _failure(
                    category="network",
                    code="transport-error",
                    description="the Provider connection failed",
                    retryable=not started,
                    action="retry" if not started else "retry manually from the last durable frame",
                    cause=type(exc).__name__,
                )
                if not started and attempt < self.max_retries:
                    await self._wait_before_retry(attempt, None)
                    attempt += 1
                    attempt_request_id = self._id_generator.new_id("request")
                    attempt_request_ids.append(attempt_request_id)
                    continue
                yield ResponseFailed(
                    _provider_attempt_failure(
                        failure,
                        attempt,
                        self.max_retries,
                        started,
                        attempt_request_ids,
                    )
                )
                return

            except Exception as exc:
                failure = _failure(
                    category="internal",
                    code="adapter-error",
                    description="the Provider adapter failed unexpectedly",
                    retryable=False,
                    action="inspect the diagnostic error ID",
                    cause=type(exc).__name__,
                )
                yield ResponseFailed(
                    _provider_attempt_failure(
                        failure,
                        attempt,
                        self.max_retries,
                        started,
                        attempt_request_ids,
                    )
                )
                return

    @asynccontextmanager
    async def _response_stream(
        self, payload: Mapping[str, object]
    ) -> AsyncIterator[httpx.Response]:
        context = self._client.stream(
            "POST",
            _chat_completions_url(self.base_url),
            headers=self._headers(),
            json=payload,
        )
        try:
            async with asyncio.timeout(self.timeouts.total):
                response = await context.__aenter__()
        except BaseException as exc:
            await context.__aexit__(type(exc), exc, exc.__traceback__)
            raise
        try:
            yield response
        except BaseException as exc:
            await context.__aexit__(type(exc), exc, exc.__traceback__)
            raise
        else:
            await context.__aexit__(None, None, None)

    async def _http_failure(self, response: httpx.Response) -> _ProviderFailure:
        body = await response.aread()
        detail = _redacted_body(body, self._api_key)
        status = response.status_code
        retry_after = _retry_after(response.headers.get("retry-after"))
        lowered = detail.lower()
        if status in {401, 403}:
            return _ProviderFailure(
                _failure(
                    category="authentication",
                    code=f"http-{status}",
                    description="the Provider rejected authentication",
                    retryable=False,
                    action="check the API key and Provider configuration",
                    cause=f"HTTP {status}",
                )
            )
        if status == 429:
            return _ProviderFailure(
                _failure(
                    category="rate-limit",
                    code="http-429",
                    description="the Provider rate limit was reached",
                    retryable=True,
                    action="retry after the Provider delay",
                    cause=f"HTTP {status}",
                ),
                retry_after=retry_after,
            )
        if status in {408, 504}:
            return _ProviderFailure(
                _failure(
                    category="provider-timeout",
                    code=f"http-{status}",
                    description="the Provider timed out the request",
                    retryable=True,
                    action="retry the request",
                    cause=f"HTTP {status}",
                ),
                retry_after=retry_after,
            )
        if 500 <= status <= 599:
            return _ProviderFailure(
                _failure(
                    category="network",
                    code=f"http-{status}",
                    description="the Provider returned a temporary server failure",
                    retryable=True,
                    action="retry the request",
                    cause=f"HTTP {status}",
                ),
                retry_after=retry_after,
            )
        if status == 400 and any(
            marker in lowered for marker in ("context length", "maximum context", "max_tokens")
        ):
            return _ProviderFailure(
                _failure(
                    category="context-overflow",
                    code="http-400-context-overflow",
                    description=(
                        "the Provider rejected the request because its context is too large"
                    ),
                    retryable=False,
                    action="reduce the Context Frame and retry",
                    cause=f"HTTP {status}",
                )
            )
        if status == 400 and _mentions_tools_unsupported(lowered):
            self._structured_tools_supported = False
            return _ProviderFailure(
                _failure(
                    category="provider-protocol",
                    code="structured-tools-unsupported",
                    description="the Provider does not support structured Tool Calls",
                    retryable=False,
                    action="use a Provider with structured Tool support",
                    cause=f"HTTP {status}",
                )
            )
        return _ProviderFailure(
            _failure(
                category="provider-protocol",
                code=f"http-{status}",
                description="the Provider rejected the Chat Completions request",
                retryable=False,
                action="check the Provider contract and request configuration",
                cause=f"HTTP {status}",
            )
        )

    def _headers(self) -> dict[str, str]:
        headers = {"Accept": "text/event-stream", "Content-Type": "application/json"}
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"
        return headers

    async def _wait_before_retry(self, attempt: int, retry_after: float | None) -> None:
        if retry_after is not None:
            delay = min(retry_after, 60.0)
        else:
            delay = (
                self.backoff_base_seconds * (2**attempt)
                + random.random() * self.backoff_base_seconds
            )
        await self._sleep(delay)

    def _events_for_chunk(
        self,
        raw: Mapping[str, object],
        state: _ResponseState,
    ) -> tuple[StreamEvent, ...]:
        if "error" in raw:
            raise _ProtocolViolation(
                _stream_failure("provider-error", "the Provider returned an error event")
            )
        raw_id = raw.get("id")
        if raw_id is not None:
            if not isinstance(raw_id, str) or not raw_id.strip():
                raise _ProtocolViolation(
                    _stream_failure("invalid-request-id", "the Provider response ID was invalid")
                )
            if state.started and raw_id != state.request_id:
                raise _ProtocolViolation(
                    _stream_failure("request-id-changed", "the Provider changed the response ID")
                )
            if not state.started:
                state.request_id = raw_id

        choices = raw.get("choices")
        usage_value = raw.get("usage")
        if choices is None:
            choices = []
        if not isinstance(choices, list):
            raise _ProtocolViolation(
                _stream_failure("choices-not-array", "the Provider choices field was invalid")
            )
        if state.finish_reason is not None and choices:
            raise _ProtocolViolation(
                _stream_failure(
                    "events-after-stop",
                    "the Provider emitted content after its stop reason",
                )
            )
        if len(choices) > 1:
            raise _ProtocolViolation(
                _stream_failure("multiple-choices", "the Provider returned multiple choices")
            )
        if not choices and usage_value is None:
            raise _ProtocolViolation(
                _stream_failure("empty-chunk", "the Provider returned an empty stream chunk")
            )

        events: list[StreamEvent] = []
        if not state.started:
            state.started = True
            events.append(ResponseStarted(state.request_id))

        if choices:
            choice = choices[0]
            if not isinstance(choice, dict):
                raise _ProtocolViolation(
                    _stream_failure("choice-not-object", "the Provider choice was invalid")
                )
            index = choice.get("index", 0)
            if index != 0:
                raise _ProtocolViolation(
                    _stream_failure("choice-index", "only the first Provider choice is supported")
                )
            delta = choice.get("delta", {})
            if not isinstance(delta, dict):
                raise _ProtocolViolation(
                    _stream_failure("delta-not-object", "the Provider delta was invalid")
                )
            if state.usage_seen and (delta.get("content") or delta.get("tool_calls")):
                raise _ProtocolViolation(
                    _stream_failure(
                        "content-after-usage", "the Provider emitted content after usage"
                    )
                )
            events.extend(self._delta_events(delta, state))
            finish_reason = choice.get("finish_reason")
            if finish_reason is not None:
                if finish_reason not in {"stop", "tool_calls"}:
                    raise _ProtocolViolation(
                        _stream_failure(
                            "unsupported-stop", "the Provider used an unsupported stop reason"
                        )
                    )
                if state.finish_reason is not None and state.finish_reason != finish_reason:
                    raise _ProtocolViolation(
                        _stream_failure("stop-changed", "the Provider changed its stop reason")
                    )
                state.finish_reason = cast(str, finish_reason)
        if usage_value is not None:
            _record_usage(usage_value, state)
        return tuple(events)

    def _delta_events(
        self,
        delta: Mapping[str, object],
        state: _ResponseState,
    ) -> list[StreamEvent]:
        events: list[StreamEvent] = []
        role = delta.get("role")
        if role is not None:
            if role != "assistant" or state.role_seen:
                raise _ProtocolViolation(
                    _stream_failure("invalid-role", "the Provider assistant role was invalid")
                )
            state.role_seen = True
        content = delta.get("content")
        if content is not None:
            if not isinstance(content, str):
                raise _ProtocolViolation(
                    _stream_failure("content-not-string", "the Provider text delta was invalid")
                )
            if content:
                events.append(TextDelta(content))
        raw_tool_calls = delta.get("tool_calls")
        if raw_tool_calls is not None:
            if not isinstance(raw_tool_calls, list):
                raise _ProtocolViolation(
                    _stream_failure(
                        "tool-calls-not-array", "the Provider Tool Calls field was invalid"
                    )
                )
            self._structured_tools_supported = True
            for raw_call in raw_tool_calls:
                events.extend(_tool_delta_events(raw_call, state))
        return events


def _request_payload(
    messages: Sequence[Message] | ContextFrame,
    *,
    model: str,
    static_tool_definitions: Sequence[Mapping[str, object] | ToolDefinition],
    fallback_request_id: str,
) -> tuple[dict[str, object], str]:
    if isinstance(messages, ContextFrame):
        provider_messages: Sequence[Message | ContextMessage] = tuple(messages.provider_messages)
        tool_values: Sequence[Mapping[str, object] | ToolDefinition] = (
            messages.tool_definitions or tuple(static_tool_definitions)
        )
        fallback_request_id = messages.manifest.request_id
    else:
        provider_messages = tuple(messages)
        tool_values = static_tool_definitions
    if not fallback_request_id.strip():
        fallback_request_id = "request-unknown"
    payload: dict[str, object] = {
        "model": model,
        "messages": [_message_payload(message) for message in provider_messages],
        "stream": True,
        "stream_options": {"include_usage": True},
    }
    if tool_values:
        payload["tools"] = [_tool_payload(value) for value in tool_values]
    return payload, fallback_request_id


def _message_payload(message: Message | ContextMessage) -> dict[str, object]:
    if isinstance(message, ContextMessage):
        role = "system" if message.role == "developer" else message.role
        if message.message is not None:
            return _message_payload_with_role(message.message, role)
        if message.role == "tool":
            raise ProviderConfigurationError(
                "a Context Frame Tool message is missing its Tool Call ID"
            )
        return {"role": role, "content": message.content}
    return _message_payload_with_role(message, message.role)


def _message_payload_with_role(message: Message, role: str) -> dict[str, object]:
    if isinstance(message, UserMessage):
        return {"role": role, "content": message.content}
    if isinstance(message, AssistantMessage):
        value: dict[str, object] = {"role": role, "content": message.content or None}
        if message.tool_calls:
            value["tool_calls"] = [
                {
                    "id": call.tool_call_id,
                    "type": "function",
                    "function": {
                        "name": call.name,
                        "arguments": json.dumps(
                            call.arguments,
                            ensure_ascii=False,
                            sort_keys=True,
                            separators=(",", ":"),
                        ),
                    },
                }
                for call in message.tool_calls
            ]
        return value
    if isinstance(message, ToolResultMessage):
        return {
            "role": "tool",
            "tool_call_id": message.tool_call_id,
            "content": message.content,
        }
    raise ProviderConfigurationError(f"unsupported Provider message: {type(message).__name__}")


def _tool_payload(value: Mapping[str, object] | ToolDefinition) -> dict[str, object]:
    if isinstance(value, ToolDefinition):
        value = cast(Mapping[str, object], value.model_dump(mode="json"))
    if value.get("type") == "function" and isinstance(value.get("function"), dict):
        return dict(value)
    name = value.get("name")
    description = value.get("description", "")
    schema = value.get("input_schema", value.get("parameters", {}))
    if not isinstance(name, str) or not name.strip():
        raise ProviderConfigurationError("Tool definition name cannot be blank")
    if not isinstance(description, str):
        raise ProviderConfigurationError("Tool definition description must be text")
    if not isinstance(schema, dict):
        raise ProviderConfigurationError("Tool definition schema must be an object")
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": dict(schema),
        },
    }


def _tool_delta_events(raw_call: object, state: _ResponseState) -> list[StreamEvent]:
    if not isinstance(raw_call, dict):
        raise _ProtocolViolation(
            _stream_failure("tool-call-not-object", "the Provider Tool Call was invalid")
        )
    index = raw_call.get("index")
    if isinstance(index, bool) or not isinstance(index, int) or index < 0:
        raise _ProtocolViolation(
            _stream_failure("tool-call-index", "the Provider Tool Call index was invalid")
        )
    accumulator = state.tool_calls.setdefault(index, _ToolAccumulator(index))
    call_id = raw_call.get("id")
    if call_id is not None:
        if not isinstance(call_id, str) or not call_id.strip():
            raise _ProtocolViolation(
                _stream_failure("tool-call-id", "the Provider Tool Call ID was invalid")
            )
        if accumulator.tool_call_id is not None and accumulator.tool_call_id != call_id:
            raise _ProtocolViolation(
                _stream_failure("tool-call-id-changed", "the Provider changed a Tool Call ID")
            )
        if any(
            other.index != index and other.tool_call_id == call_id
            for other in state.tool_calls.values()
        ):
            raise _ProtocolViolation(
                _stream_failure("duplicate-tool-call-id", "the Provider reused a Tool Call ID")
            )
        accumulator.tool_call_id = call_id
    call_type = raw_call.get("type")
    if call_type is not None and call_type != "function":
        raise _ProtocolViolation(
            _stream_failure("tool-call-type", "only function Tool Calls are supported")
        )
    function = raw_call.get("function", {})
    if not isinstance(function, dict):
        raise _ProtocolViolation(
            _stream_failure("tool-function-not-object", "the Provider function payload was invalid")
        )
    name = function.get("name")
    if name is not None:
        if not isinstance(name, str) or not name.strip():
            raise _ProtocolViolation(
                _stream_failure("tool-name", "the Provider Tool name was invalid")
            )
        if accumulator.name is not None and accumulator.name != name:
            raise _ProtocolViolation(
                _stream_failure("tool-name-changed", "the Provider changed a Tool name")
            )
        accumulator.name = name
    arguments = function.get("arguments")
    if arguments is not None:
        if not isinstance(arguments, str):
            raise _ProtocolViolation(
                _stream_failure("tool-arguments", "the Provider Tool arguments were invalid")
            )
        if accumulator.argument_fragments is None:
            raise _ProtocolViolation(
                _stream_failure(
                    "tool-arguments-state", "the Provider Tool argument state was invalid"
                )
            )
        accumulator.argument_fragments.append(arguments)

    events: list[StreamEvent] = []
    if not accumulator.started and accumulator.name is not None:
        accumulator.tool_call_id = accumulator.tool_call_id or (
            f"{state.request_id}:tool:{accumulator.index}"
        )
        accumulator.started = True
        events.append(ToolCallStarted(accumulator.tool_call_id, accumulator.name))
        fragments = accumulator.argument_fragments or []
        for fragment in fragments:
            if fragment:
                events.append(ToolCallArgumentDelta(accumulator.tool_call_id, fragment))
        accumulator.emitted_fragments = len(fragments)
    elif accumulator.started:
        fragments = accumulator.argument_fragments or []
        for fragment in fragments[accumulator.emitted_fragments :]:
            if fragment:
                events.append(ToolCallArgumentDelta(accumulator.tool_call_id or "", fragment))
        accumulator.emitted_fragments = len(fragments)
    return events


def _record_usage(value: object, state: _ResponseState) -> None:
    if state.usage_seen:
        raise _ProtocolViolation(
            _stream_failure("duplicate-usage", "the Provider reported usage twice")
        )
    if not isinstance(value, dict):
        raise _ProtocolViolation(
            _stream_failure("usage-not-object", "the Provider usage payload was invalid")
        )
    input_tokens = value.get("prompt_tokens")
    output_tokens = value.get("completion_tokens")
    if (
        isinstance(input_tokens, bool)
        or not isinstance(input_tokens, int)
        or input_tokens < 0
        or isinstance(output_tokens, bool)
        or not isinstance(output_tokens, int)
        or output_tokens < 0
    ):
        raise _ProtocolViolation(
            _stream_failure("usage-fields", "the Provider usage counts were invalid")
        )
    state.usage_seen = True
    state.usage = (input_tokens, output_tokens)


def _finish_response(state: _ResponseState) -> tuple[StreamEvent, ...]:
    if not state.started:
        raise _ProtocolViolation(
            _stream_failure("no-response", "the Provider ended before a response started")
        )
    if state.finish_reason is None:
        raise _ProtocolViolation(
            _stream_failure("missing-stop", "the Provider stream ended without a stop reason")
        )
    calls = tuple(state.tool_calls[index] for index in sorted(state.tool_calls))
    if state.finish_reason == "stop" and calls:
        raise _ProtocolViolation(
            _stream_failure("stop-with-tools", "the Provider stopped with incomplete Tool Calls")
        )
    if state.finish_reason == "tool_calls" and not calls:
        raise _ProtocolViolation(
            _stream_failure(
                "tool-stop-without-tools", "the Provider announced Tool Calls without any calls"
            )
        )
    events: list[StreamEvent] = []
    for call in calls:
        if not call.started or call.tool_call_id is None or call.name is None:
            raise _ProtocolViolation(
                _stream_failure("partial-tool-call", "the Provider emitted an incomplete Tool Call")
            )
        try:
            arguments = json.loads(call.arguments or "{}")
        except json.JSONDecodeError as exc:
            raise _ProtocolViolation(
                _stream_failure(
                    "invalid-tool-json", "the Provider Tool arguments were not valid JSON"
                )
            ) from exc
        if not isinstance(arguments, dict):
            raise _ProtocolViolation(
                _stream_failure(
                    "tool-json-object", "the Provider Tool arguments were not an object"
                )
            )
        events.append(ToolCallCompleted(call.tool_call_id))
    if state.usage is not None:
        events.append(UsageReported(*state.usage))
    events.append(ResponseCompleted(cast(Any, state.finish_reason)))
    return tuple(events)


async def _iter_sse_data(
    response: httpx.Response,
    timeouts: ProviderTimeouts,
    *,
    deadline: float | None = None,
) -> AsyncIterator[str]:
    lines = response.aiter_lines().__aiter__()
    data_lines: list[str] = []
    saw_data = False
    deadline = deadline or time.monotonic() + timeouts.total
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise _ProviderTimeout(
                _stream_failure(
                    "total-timeout",
                    "the Provider request exceeded its total timeout",
                    retryable=not saw_data,
                )
            )
        limit = timeouts.first_event if not saw_data else timeouts.idle
        try:
            async with asyncio.timeout(min(limit, remaining)):
                line = await lines.__anext__()
        except StopAsyncIteration as exc:
            raise _ProtocolViolation(
                _stream_failure("unexpected-eof", "the Provider stream ended before [DONE]")
            ) from exc
        except TimeoutError as exc:
            total_expired = remaining <= limit
            code = (
                "total-timeout"
                if total_expired
                else "first-event-timeout"
                if not saw_data
                else "idle-timeout"
            )
            description = (
                "the Provider request exceeded its total timeout"
                if total_expired
                else "the Provider did not emit its first event before the timeout"
                if not saw_data
                else "the Provider stream was idle beyond the timeout"
            )
            raise _ProviderTimeout(
                _stream_failure(code, description, retryable=not saw_data)
            ) from exc
        if line == "":
            if data_lines:
                saw_data = True
                yield "\n".join(data_lines)
                data_lines = []
            continue
        if line.startswith(":"):
            continue
        if line.startswith("data:"):
            data_lines.append(line[5:].lstrip())
            continue
        if line.startswith("event:"):
            event_name = line[6:].strip()
            if event_name not in {"", "message"}:
                raise _ProtocolViolation(
                    _stream_failure(
                        "unknown-sse-event", "the Provider emitted an unknown SSE event"
                    )
                )
            continue
        if line.startswith("id:") or line.startswith("retry:"):
            continue
        raise _ProtocolViolation(
            _stream_failure("unknown-sse-field", "the Provider emitted an unknown SSE field")
        )


def _decode_chunk(data: str) -> Mapping[str, object]:
    if not data.strip():
        raise _ProtocolViolation(
            _stream_failure("empty-data", "the Provider emitted an empty SSE data event")
        )
    try:
        value = json.loads(data)
    except json.JSONDecodeError as exc:
        raise _ProtocolViolation(
            _stream_failure("invalid-json", "the Provider emitted invalid JSON")
        ) from exc
    if not isinstance(value, dict):
        raise _ProtocolViolation(
            _stream_failure("json-not-object", "the Provider event was not a JSON object")
        )
    return cast(Mapping[str, object], value)


def _stream_failure(
    code: str,
    description: str,
    *,
    retryable: bool = False,
) -> Failure:
    return _failure(
        category="provider-protocol"
        if code not in {"first-event-timeout", "idle-timeout", "total-timeout"}
        else "provider-timeout",
        code=code,
        description=description,
        retryable=retryable,
        action="inspect the Provider contract" if not retryable else "retry the request",
        cause="normalized Provider stream validation",
    )


def _failure(
    *,
    category: str,
    code: str,
    description: str,
    retryable: bool,
    action: str,
    cause: str | None,
) -> Failure:
    return Failure(
        category=category,
        code=code,
        source="openai-compatible-provider",
        redacted_description=description,
        retryable=retryable,
        required_user_action=action,
        cause=cause,
    )


def _provider_attempt_failure(
    failure: Failure,
    attempt: int,
    max_retries: int,
    started: bool,
    attempt_request_ids: list[str],
) -> Failure:
    """Expose provider-level retry exhaustion to the outer Agent Loop."""

    if started or not failure.retryable:
        return failure
    details = dict(failure.details)
    details.update(
        {
            "provider_attempts": attempt + 1,
            "automatic_retries_exhausted": attempt >= max_retries,
            "provider_request_ids": list(attempt_request_ids),
        }
    )
    return replace(failure, details=details)


def _validated_base_url(value: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ProviderConfigurationError("Provider Base URL cannot be blank")
    parsed = urlsplit(value.strip())
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ProviderConfigurationError("Provider Base URL must be an HTTP(S) URL")
    if parsed.username is not None or parsed.password is not None:
        raise ProviderConfigurationError("Provider Base URL must not contain credentials")
    return value.strip().rstrip("/")


def _chat_completions_url(base_url: str) -> str:
    return base_url if base_url.endswith("/chat/completions") else f"{base_url}/chat/completions"


def _redacted_body(body: bytes, secret: str | None) -> str:
    text = body[:4096].decode("utf-8", errors="replace")
    return redact_secrets(text, (secret,) if secret else ())[:500]


def _retry_after(value: str | None) -> float | None:
    if value is None:
        return None
    try:
        parsed = float(value)
    except ValueError:
        return None
    return parsed if parsed >= 0 else None


def _mentions_tools_unsupported(value: str) -> bool:
    return "tool" in value and any(
        word in value for word in ("unsupported", "not support", "unknown")
    )


# Names used by callers that prefer the shorter adapter spelling.
OpenAICompatibleProvider = OpenAICompatibleModelProvider


__all__ = [
    "OpenAICompatibleModelProvider",
    "OpenAICompatibleProvider",
    "ProviderCapabilities",
    "ProviderConfigurationError",
    "ProviderTimeouts",
]
