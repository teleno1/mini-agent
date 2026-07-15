# 05 - Stream a real OpenAI-compatible text response

**What to build:** A configured user can run a text-only Turn against the supported OpenAI-compatible Chat Completions subset and see honest, bounded streaming behavior through the existing Provider-neutral seam.

**Blocked by:** 04 - Assemble trusted configuration, instructions, and Context Frames.

**Status:** completed

- [x] Implement the direct httpx Chat Completions adapter with configurable user-controlled Base URL and model plus structured-tool capability detection.
- [x] Translate Provider chunks into the complete normalized Stream Event vocabulary with legal ordering and stable request/Tool Call IDs.
- [x] Persist only a successfully closed aggregate; deltas remain ephemeral and a broken stream remains visibly incomplete.
- [x] Discard partial Tool arguments and reject unknown or illegal Provider events as redacted protocol Failures.
- [x] Apply bounded renderer backpressure, text-refresh coalescing, and plain-text fallback without changing aggregate content.
- [x] Enforce connect, first-event, idle, and total request timeouts and map Provider responses into the stable Failure categories.
- [x] Contract tests prove Fake and real adapters preserve role authority, Tool schema, message pairing, and partial-stream semantics without real calls in CI.

## Completion evidence

- Direct adapter, configurable endpoint/model, headers, request payload, and capability detection: `tests/test_provider.py::test_real_adapter_normalizes_text_stream_and_request_headers`, `tests/test_provider.py::test_real_adapter_preserves_structured_tools_and_message_pairing`, and `tests/test_provider.py::test_real_adapter_normalizes_tool_call_deltas_and_stable_ids`.
- Complete normalized stream vocabulary, stable IDs, legal ordering, and post-stop rejection: `tests/test_provider.py::test_real_adapter_normalizes_text_stream_and_request_headers`, `tests/test_provider.py::test_real_adapter_normalizes_tool_call_deltas_and_stable_ids`, and `tests/test_provider.py::test_real_adapter_rejects_content_after_provider_stop_reason`.
- Durable-before-success behavior with ephemeral partial text and no assistant message on a broken stream: `tests/test_provider.py::test_broken_real_stream_renders_partial_text_but_never_persists_assistant_message` and `tests/test_sessions.py::test_failed_stream_persists_failure_without_an_assistant_message`.
- Partial Tool arguments are never completed or persisted; protocol failures are bounded/redacted: `tests/test_provider.py::test_partial_tool_arguments_are_reported_as_redacted_protocol_failure`.
- Bounded renderer queue, aggregate preservation, coalescing, and plain fallback: `tests/test_provider.py::test_renderer_coalesces_text_applies_backpressure_and_falls_back_plain`.
- Connect/first-event/idle/total timeout configuration and stable HTTP Failure categories with bounded pre-stream retry: `tests/test_provider.py::test_first_event_timeout_is_bounded_and_retryable_without_partial_events`, `tests/test_provider.py::test_idle_and_total_stream_timeouts_are_distinct_and_bounded`, and `tests/test_provider.py::test_provider_maps_authentication_and_retries_pre_stream_5xx`; total deadline also covers response establishment in `OpenAICompatibleModelProvider._response_stream`.
- Fake/real normalized-stream and Context Frame contract parity without network calls: `tests/test_provider.py::test_fake_and_real_adapters_share_stream_and_context_contracts[fake]` and `[real]`, using `httpx.MockTransport`; all provider tests run offline.
- Full verification: `uv run pytest` (67 passed, 2 platform-capability skips), `uv run ruff check src tests`, `uv run mypy`, `uv build`, and `git diff --check`.
