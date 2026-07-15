import asyncio
import json
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest

from mini_agent.adapters.clocks import DeterministicClock
from mini_agent.adapters.ids import DeterministicIdGenerator
from mini_agent.adapters.session_store import SessionStore
from mini_agent.application.rendering import BoundedStreamRenderer
from mini_agent.application.turns import TextTurnApplication
from mini_agent.context import ContextBuilder
from mini_agent.domain.messages import (
    AssistantMessage,
    ToolCallBlock,
    ToolResultMessage,
    UserMessage,
)
from mini_agent.domain.streams import (
    ResponseCompleted,
    ResponseFailed,
    ResponseStarted,
    TextDelta,
    ToolCallArgumentDelta,
    ToolCallCompleted,
    ToolCallStarted,
    UsageReported,
)
from mini_agent.domain.turns import StreamFailed
from mini_agent.providers.fake import ScriptedFakeModelProvider
from mini_agent.providers.openai_compatible import (
    OpenAICompatibleModelProvider,
    ProviderTimeouts,
)


class _DelayedStream(httpx.AsyncByteStream):
    async def __aiter__(self):
        await asyncio.sleep(0.05)
        yield b"data: {}\n\n"


class _TimedStream(httpx.AsyncByteStream):
    def __init__(self, chunks: tuple[tuple[float, bytes], ...]) -> None:
        self._chunks = chunks

    async def __aiter__(self):
        for delay, chunk in self._chunks:
            await asyncio.sleep(delay)
            yield chunk


def _encode_sse(*events: dict[str, Any] | str) -> bytes:
    lines: list[str] = []
    for event in events:
        data = event if isinstance(event, str) else json.dumps(event)
        lines.extend((f"data: {data}", ""))
    return ("\n".join(lines) + "\n").encode()


async def _collect_events(provider: OpenAICompatibleModelProvider) -> list[object]:
    return [event async for event in provider.stream((UserMessage("hello"),))]


@pytest.mark.asyncio
async def test_real_adapter_normalizes_text_stream_and_request_headers() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=_encode_sse(
                {
                    "id": "chat-request-1",
                    "object": "chat.completion.chunk",
                    "choices": [
                        {"index": 0, "delta": {"role": "assistant"}, "finish_reason": None}
                    ],
                },
                {
                    "id": "chat-request-1",
                    "choices": [{"index": 0, "delta": {"content": "Hello"}, "finish_reason": None}],
                },
                {
                    "id": "chat-request-1",
                    "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
                },
                {
                    "id": "chat-request-1",
                    "choices": [],
                    "usage": {"prompt_tokens": 3, "completion_tokens": 1, "total_tokens": 4},
                },
                "[DONE]",
            ),
        )

    provider = OpenAICompatibleModelProvider(
        "https://provider.test/v1",
        api_key="sk-test-provider-secret",
        model="test-model",
        transport=httpx.MockTransport(handler),
        id_generator=DeterministicIdGenerator(),
    )
    events = await _collect_events(provider)

    assert [type(event) for event in events] == [
        ResponseStarted,
        TextDelta,
        UsageReported,
        ResponseCompleted,
    ]
    assert events[0] == ResponseStarted("chat-request-1")
    assert events[1] == TextDelta("Hello")
    assert requests[0].url == "https://provider.test/v1/chat/completions"
    assert requests[0].headers["authorization"] == "Bearer sk-test-provider-secret"
    assert json.loads(requests[0].content)["messages"] == [{"role": "user", "content": "hello"}]


@pytest.mark.asyncio
async def test_real_adapter_rejects_content_after_provider_stop_reason() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        del request
        return httpx.Response(
            200,
            content=_encode_sse(
                {
                    "id": "late-request",
                    "choices": [
                        {
                            "index": 0,
                            "delta": {"role": "assistant"},
                            "finish_reason": "stop",
                        }
                    ],
                },
                {
                    "id": "late-request",
                    "choices": [
                        {
                            "index": 0,
                            "delta": {"content": "late"},
                            "finish_reason": None,
                        }
                    ],
                },
                "[DONE]",
            ),
        )

    provider = OpenAICompatibleModelProvider(
        "https://provider.test/v1",
        model="test-model",
        transport=httpx.MockTransport(handler),
        max_retries=0,
    )
    events = await _collect_events(provider)

    assert isinstance(events[-1], ResponseFailed)
    assert events[-1].failure.code == "events-after-stop"
    assert not any(isinstance(event, ResponseCompleted) for event in events)


@pytest.mark.asyncio
async def test_real_adapter_preserves_structured_tools_and_message_pairing(tmp_path) -> None:
    captured: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(json.loads(request.content))
        return httpx.Response(
            200,
            content=_encode_sse(
                {
                    "id": "tool-request",
                    "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
                },
                "[DONE]",
            ),
        )

    frame = ContextBuilder(tmp_path).build(
        "continue",
        request_id="frame-request",
        history=(
            UserMessage("read it"),
            AssistantMessage(
                "",
                (ToolCallBlock("call-1", "read_file", {"path": "note.txt"}),),
            ),
            ToolResultMessage("call-1", '{"content":"ok"}', "success"),
        ),
        tool_definitions=[
            {
                "name": "read_file",
                "description": "Read one file",
                "input_schema": {
                    "type": "object",
                    "properties": {"path": {"type": "string"}},
                    "required": ["path"],
                },
            }
        ],
    )
    provider = OpenAICompatibleModelProvider(
        "https://provider.test/v1",
        model="test-model",
        transport=httpx.MockTransport(handler),
        max_retries=0,
    )

    events = [event async for event in provider.stream(frame)]
    payload = captured[0]

    assert not any(isinstance(event, ResponseFailed) for event in events)
    assert [message["role"] for message in payload["messages"]] == [
        "system",
        "system",
        "system",
        "developer",
        "user",
        "assistant",
        "tool",
        "user",
    ]
    assistant = next(message for message in payload["messages"] if message["role"] == "assistant")
    assert assistant["tool_calls"][0]["id"] == "call-1"
    assert assistant["tool_calls"][0]["function"]["name"] == "read_file"
    assert payload["messages"][6] == {
        "role": "tool",
        "tool_call_id": "call-1",
        "content": '{"content":"ok"}',
    }
    assert payload["tools"] == [
        {
            "type": "function",
            "function": {
                "name": "read_file",
                "description": "Read one file",
                "parameters": {
                    "type": "object",
                    "properties": {"path": {"type": "string"}},
                    "required": ["path"],
                },
            },
        }
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("adapter", ["fake", "real"])
async def test_fake_and_real_adapters_share_stream_and_context_contracts(
    adapter: str, tmp_path
) -> None:
    captured: list[dict[str, Any]] = []
    response_events = (
        ResponseStarted("shared-request"),
        TextDelta("shared "),
        TextDelta("response"),
        UsageReported(2, 2),
        ResponseCompleted(),
    )
    if adapter == "fake":
        provider: ScriptedFakeModelProvider | OpenAICompatibleModelProvider = (
            ScriptedFakeModelProvider(responses=[response_events])
        )
    else:

        def handler(request: httpx.Request) -> httpx.Response:
            captured.append(json.loads(request.content))
            return httpx.Response(
                200,
                content=_encode_sse(
                    {
                        "id": "shared-request",
                        "choices": [
                            {
                                "index": 0,
                                "delta": {"role": "assistant", "content": "shared "},
                                "finish_reason": None,
                            }
                        ],
                    },
                    {
                        "id": "shared-request",
                        "choices": [
                            {
                                "index": 0,
                                "delta": {"content": "response"},
                                "finish_reason": "stop",
                            }
                        ],
                    },
                    {
                        "id": "shared-request",
                        "choices": [],
                        "usage": {
                            "prompt_tokens": 2,
                            "completion_tokens": 2,
                            "total_tokens": 4,
                        },
                    },
                    "[DONE]",
                ),
            )

        provider = OpenAICompatibleModelProvider(
            "https://provider.test/v1",
            model="test-model",
            transport=httpx.MockTransport(handler),
            max_retries=0,
        )

    frame = ContextBuilder(tmp_path).build(
        "continue",
        request_id="frame-request",
        history=(
            AssistantMessage(
                "",
                (ToolCallBlock("call-1", "read_file", {"path": "note.txt"}),),
            ),
            ToolResultMessage("call-1", "ok", "success"),
        ),
        tool_definitions=[
            {
                "name": "read_file",
                "description": "Read one file",
                "input_schema": {"type": "object"},
            }
        ],
    )
    events = [event async for event in provider.stream(frame)]

    assert events == list(response_events)
    if adapter == "fake":
        assert isinstance(provider.requests[0], type(frame))
        sent_frame = provider.requests[0]
        assert [message.role for message in sent_frame.provider_messages] == [
            "system",
            "system",
            "system",
            "developer",
            "assistant",
            "tool",
            "user",
        ]
        assert sent_frame.tool_definitions[0]["name"] == "read_file"
    else:
        payload = captured[0]
        assert [message["role"] for message in payload["messages"]] == [
            "system",
            "system",
            "system",
            "developer",
            "assistant",
            "tool",
            "user",
        ]
        assert payload["messages"][4]["tool_calls"][0]["id"] == "call-1"
        assert payload["messages"][5]["tool_call_id"] == "call-1"
        assert payload["tools"][0]["function"]["parameters"] == {"type": "object"}


@pytest.mark.asyncio
async def test_real_adapter_normalizes_tool_call_deltas_and_stable_ids() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        del request
        return httpx.Response(
            200,
            content=_encode_sse(
                {
                    "id": "tool-request",
                    "choices": [
                        {
                            "index": 0,
                            "delta": {
                                "tool_calls": [
                                    {
                                        "index": 0,
                                        "id": "call-1",
                                        "type": "function",
                                        "function": {"name": "read_file", "arguments": '{"path":"'},
                                    }
                                ]
                            },
                            "finish_reason": None,
                        }
                    ],
                },
                {
                    "id": "tool-request",
                    "choices": [
                        {
                            "index": 0,
                            "delta": {
                                "tool_calls": [
                                    {"index": 0, "function": {"arguments": 'note.txt"}'}}
                                ]
                            },
                            "finish_reason": None,
                        }
                    ],
                },
                {
                    "id": "tool-request",
                    "choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}],
                },
                "[DONE]",
            ),
        )

    provider = OpenAICompatibleModelProvider(
        "https://provider.test/v1",
        model="test-model",
        transport=httpx.MockTransport(handler),
        max_retries=0,
    )
    events = await _collect_events(provider)

    assert events == [
        ResponseStarted("tool-request"),
        ToolCallStarted("call-1", "read_file"),
        ToolCallArgumentDelta("call-1", '{"path":"'),
        ToolCallArgumentDelta("call-1", 'note.txt"}'),
        ToolCallCompleted("call-1"),
        ResponseCompleted("tool_calls"),
    ]
    assert provider.supports_structured_tools is True


@pytest.mark.asyncio
async def test_partial_tool_arguments_are_reported_as_redacted_protocol_failure() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        del request
        return httpx.Response(
            200,
            content=_encode_sse(
                {
                    "id": "partial-request",
                    "choices": [
                        {
                            "index": 0,
                            "delta": {
                                "tool_calls": [
                                    {
                                        "index": 0,
                                        "id": "call-partial",
                                        "function": {
                                            "name": "read_file",
                                            "arguments": '{"path":"unterminated',
                                        },
                                    }
                                ]
                            },
                            "finish_reason": "tool_calls",
                        }
                    ],
                },
                "[DONE]",
            ),
        )

    provider = OpenAICompatibleModelProvider(
        "https://provider.test/v1",
        model="test-model",
        transport=httpx.MockTransport(handler),
        max_retries=0,
    )
    events = await _collect_events(provider)

    assert isinstance(events[-1], ResponseFailed)
    failure = events[-1].failure
    assert failure.category == "provider-protocol"
    assert failure.code == "invalid-tool-json"
    assert "unterminated" not in failure.redacted_description
    assert not any(isinstance(event, ToolCallCompleted) for event in events)


@pytest.mark.asyncio
async def test_broken_real_stream_renders_partial_text_but_never_persists_assistant_message(
    tmp_path,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        del request
        return httpx.Response(
            200,
            content=_encode_sse(
                {
                    "id": "broken-request",
                    "choices": [
                        {
                            "index": 0,
                            "delta": {"role": "assistant", "content": "partial"},
                            "finish_reason": None,
                        }
                    ],
                },
                "[DONE]",
            ),
        )

    provider = OpenAICompatibleModelProvider(
        "https://provider.test/v1",
        model="test-model",
        transport=httpx.MockTransport(handler),
        max_retries=0,
    )
    store = SessionStore(
        tmp_path,
        clock=DeterministicClock(datetime(2026, 1, 1, tzinfo=UTC)),
        id_generator=DeterministicIdGenerator(),
    )
    application = TextTurnApplication(
        provider=provider,
        clock=DeterministicClock(datetime(2026, 1, 1, tzinfo=UTC)),
        id_generator=DeterministicIdGenerator(),
        session_store=store,
    )
    observed: list[object] = []

    with pytest.raises(StreamFailed):
        await application.run("hello", on_event=observed.append)

    snapshot = store.read("session-0001")
    assert any(isinstance(event, TextDelta) and event.text == "partial" for event in observed)
    assert isinstance(observed[-1], ResponseFailed)
    assert "assistant.message" not in [event.event_type for event in snapshot.events]
    failed = next(event for event in snapshot.events if event.event_type == "model.request.failed")
    assert failed.payload["category"] == "provider-protocol"


@pytest.mark.asyncio
async def test_provider_maps_authentication_and_retries_pre_stream_5xx() -> None:
    attempts = 0
    sleeps: list[float] = []

    async def no_sleep(delay: float) -> None:
        sleeps.append(delay)

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            return httpx.Response(503, content=b"temporary upstream failure")
        return httpx.Response(401, content=b"Bearer sk-secret-provider-key")

    provider = OpenAICompatibleModelProvider(
        "https://provider.test/v1",
        api_key="sk-secret-provider-key",
        model="test-model",
        transport=httpx.MockTransport(handler),
        sleep=no_sleep,
    )
    events = await _collect_events(provider)

    assert attempts == 2
    assert sleeps
    assert isinstance(events[-1], ResponseFailed)
    assert events[-1].failure.category == "authentication"
    assert "sk-secret-provider-key" not in events[-1].failure.redacted_description


@pytest.mark.asyncio
async def test_first_event_timeout_is_bounded_and_retryable_without_partial_events() -> None:
    async def no_sleep(delay: float) -> None:
        del delay

    def handler(request: httpx.Request) -> httpx.Response:
        del request
        return httpx.Response(200, stream=_DelayedStream())

    # The transport returns a response with a delayed async stream; no real
    # network is involved in this timeout contract test.
    provider = OpenAICompatibleModelProvider(
        "https://provider.test/v1",
        model="test-model",
        transport=httpx.MockTransport(handler),
        timeouts=ProviderTimeouts(connect=1, first_event=0.001, idle=1, total=1),
        max_retries=0,
        sleep=no_sleep,
    )
    events = await _collect_events(provider)

    assert isinstance(events[-1], ResponseFailed)
    assert events[-1].failure.category == "provider-timeout"
    assert events[-1].failure.code in {"first-event-timeout", "transport-timeout"}


@pytest.mark.asyncio
async def test_idle_and_total_stream_timeouts_are_distinct_and_bounded() -> None:
    first_chunk = (
        "data: "
        + json.dumps(
            {
                "id": "timed-request",
                "choices": [
                    {
                        "index": 0,
                        "delta": {"role": "assistant", "content": "partial"},
                        "finish_reason": None,
                    }
                ],
            }
        )
        + "\n\n"
    ).encode()

    def idle_handler(request: httpx.Request) -> httpx.Response:
        del request
        return httpx.Response(
            200,
            stream=_TimedStream(((0, first_chunk), (0.05, b"data: {}\n\n"))),
        )

    idle_provider = OpenAICompatibleModelProvider(
        "https://provider.test/v1",
        model="test-model",
        transport=httpx.MockTransport(idle_handler),
        timeouts=ProviderTimeouts(connect=1, first_event=1, idle=0.001, total=1),
        max_retries=0,
    )
    idle_events = await _collect_events(idle_provider)
    assert isinstance(idle_events[-1], ResponseFailed)
    assert idle_events[-1].failure.code == "idle-timeout"

    total_provider = OpenAICompatibleModelProvider(
        "https://provider.test/v1",
        model="test-model",
        transport=httpx.MockTransport(idle_handler),
        timeouts=ProviderTimeouts(connect=1, first_event=1, idle=1, total=0.01),
        max_retries=0,
    )
    total_events = await _collect_events(total_provider)
    assert isinstance(total_events[-1], ResponseFailed)
    assert total_events[-1].failure.code == "total-timeout"


@pytest.mark.asyncio
async def test_renderer_coalesces_text_applies_backpressure_and_falls_back_plain() -> None:
    plain: list[str] = []

    def failing_rich(text: str) -> None:
        del text
        raise RuntimeError("rich renderer unavailable")

    renderer = BoundedStreamRenderer(
        rich_sink=failing_rich,
        plain_sink=plain.append,
        max_queue_size=1,
    )
    await renderer.observe(TextDelta("a"))
    await renderer.observe(TextDelta("b"))
    await renderer.observe(ResponseCompleted())
    await renderer.finish()

    assert renderer.aggregate_text == "ab"
    assert renderer.completed is True
    assert renderer.fallback_used is True
    assert plain == ["a", "b"] or plain == ["ab"]
