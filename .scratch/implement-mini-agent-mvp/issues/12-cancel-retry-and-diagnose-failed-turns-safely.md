# 12 - Cancel, retry, and diagnose failed Turns safely

**What to build:** Users can cancel active work promptly, transient model failures retry only when safe, persistence uncertainty halts execution, and every failure is understandable through redacted correlated diagnostics.

**Blocked by:** 05 - Stream a real OpenAI-compatible text response; 08 - Run bounded Shell commands under Permission Policy; 09 - Complete bounded serial multi-Tool coding Turns.

**Status:** completed

- [x] Implement the complete stable Failure taxonomy with redacted details, retryability, user action, source, cause, and correlation IDs.
- [x] Retry only agreed pre-output transient Provider failures within three total attempts, respecting Retry-After or jittered backoff and assigning a new request ID each time.
- [x] Enforce layered Provider/Tool timeouts and never automatically retry partial streams, persistence errors, cancellation, or started side effects.
- [x] First Ctrl+C acknowledges within one second, stops new scheduling, and allows five seconds for cleanup; a second forces exit 130 after best-effort recording.
- [x] If a prerequisite or terminal Session Event cannot persist, stop the Agent Loop and never report durable success; diagnostic-log failure must not hide the primary Failure.
- [x] Write redacted correlated rotating diagnostic logs and make `doctor` resolve an error ID without exposing secrets, prompts, or raw sensitive output.
- [x] Tests cover retry budgets, cancellation in streaming/permission/Shell phases, forced interrupt, broken stdout, plain-text degradation, fsync failures, exit codes, and single terminal-state invariants.

## Completion evidence

- Stable categories, redacted details, causal fields, and Session/Turn/request/Tool Call/failure IDs: `src/mini_agent/domain/streams.py`, `src/mini_agent/diagnostics.py`, and `tests/test_ticket12.py::test_failure_taxonomy_and_diagnostic_lookup_are_redacted`.
- Safe retry budget, Retry-After, new request IDs, and no partial-stream replay: `src/mini_agent/application/agent.py`, `src/mini_agent/application/turns.py`, `src/mini_agent/providers/openai_compatible.py`, `tests/test_ticket12.py::test_retry_budget_is_three_total_requests_with_new_request_ids`, `test_retry_after_is_used_and_partial_stream_is_never_retried`, and `test_text_turn_also_retries_only_before_output`.
- Provider connect/first-event/idle/total limits and Tool timeout-to-interrupted normalization: existing `tests/test_provider.py::test_first_event_timeout_is_bounded_and_retryable_without_partial_events`, `test_idle_and_total_stream_timeouts_are_distinct_and_bounded`, `tests/test_ticket12.py::test_started_tool_timeout_is_interrupted_and_not_retried`, plus Shell timeout/cancellation coverage in `tests/test_ticket08.py`.
- Streaming and async permission cancellation, no new scheduling, forced interrupt state/exit 130, and one terminal Tool result: `src/mini_agent/application/cancellation.py`, `src/mini_agent/cli/app.py`, `tests/test_ticket12.py::test_cancellation_during_streaming_closes_request_and_turn`, `test_async_permission_cancellation_has_one_terminal_tool_result`, and `test_first_interrupt_acknowledges_and_second_marks_forced_exit`.
- fsync rollback, persistence uncertainty, and no durable success after failed Session writes: `src/mini_agent/adapters/session_store.py`, `tests/test_ticket12.py::test_fsync_failure_is_a_persistence_failure_not_success`, and existing Session durability tests.
- Rotating redacted JSONL diagnostics and `doctor` lookup: `src/mini_agent/diagnostics.py`, `src/mini_agent/cli/app.py`, and `tests/test_ticket12.py::test_failure_taxonomy_and_diagnostic_lookup_are_redacted`.
- Broken stdout and plain-text fallback: `tests/test_ticket12.py::test_broken_stdout_observer_does_not_change_durable_success` and `tests/test_provider.py::test_renderer_coalesces_text_applies_backpressure_and_falls_back_plain`.
- Exit code 2 configuration handling and terminal-state invariants: `tests/test_ticket12.py::test_cli_configuration_failure_uses_exit_code_two`, cancellation tests above, and existing Agent/Session lifecycle tests.
- Final verification: `uv run --frozen pytest -q` => `118 passed, 2 skipped`; `ruff format --check`, `ruff check`, `mypy`, `uv build`, and `git diff --check` passed.
