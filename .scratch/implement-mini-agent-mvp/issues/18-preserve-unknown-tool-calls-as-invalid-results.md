# 18 - Preserve unknown Tool calls as invalid results

**What to build:** When a Model Provider requests an unknown Tool, the Coding Agent records one protocol-linked invalid Tool Result and returns that observation to the model so it can recover. An ordinary model-selected name error must not become an internal Turn failure or execute any side effect.

**Blocked by:** None - can start immediately.

**Status:** completed

- [x] An unknown Tool name terminates its persisted Tool Call exactly once with outcome `invalid` and a stable validation category.
- [x] No Tool implementation, permission confirmation, Workspace mutation, or Shell process is invoked for the unknown call.
- [x] The invalid Tool Result remains linked to the original Tool Call ID and is included in the next derived Context Frame.
- [x] The Agent Loop can accept a corrected Tool Call or a normal final response after the invalid observation without failing the Turn.
- [x] Normal Turn execution and interrupted-work retry paths do not perform a second registry lookup that converts the invalid result into an exception.
- [x] Deterministic Fake Provider tests verify durable event ordering, correction recovery, budget accounting, and absence of side effects for unknown Tool names.

## Completion evidence

Record concrete test names, event sequences, and side-effect assertions here before marking this ticket completed.

- `tests/test_ticket18.py::test_unknown_tool_is_invalid_once_and_corrected_call_recovers` verifies the durable sequence `session.created → turn.started → user.message → context.manifest.recorded → model.request.started → model.request.completed → assistant.message → tool.proposed → tool.failed → context.manifest.recorded → model.request.started → model.request.completed → assistant.message → tool.proposed → tool.validated → tool.started → tool.completed → context.manifest.recorded → model.request.started → model.request.completed → assistant.message → turn.completed`. The unknown call has exactly one `tool.failed` terminal result with outcome `invalid`, category `tool-validation`, code `unknown-tool`, and its original Call ID.
- The same test asserts `causation_id` links the invalid result to `tool.proposed`, the next `ContextFrame` contains a `ToolResultMessage` for `call-unknown`, and correction recovery completes with outcomes `invalid, success`, 3 model requests, 2 Tool Calls, and token usage `(15, 18)`.
- Side-effect assertions cover one registry lookup for `missing_tool`, no permission request for it, one execution only for the corrected `read_file`, no new file, unchanged Workspace content, and a patched Shell process launcher that is never reached. The unknown call has no `tool.validated` or `tool.started` event.
- `tests/test_ticket18.py::test_retry_interrupted_unknown_tool_returns_invalid_without_second_lookup` verifies Resume retry closes the old call once as `interrupted`, proposes a new Call ID, performs only one lookup, and records the new unknown call as `tool.failed`/`invalid` without permission or Workspace side effects.
- `tests/test_ticket18.py::test_known_invalid_tool_still_uses_registered_output_bound` preserves the registered Tool output limit for known invalid calls.
- Final verification: `157 passed, 2 skipped`; Ruff check and format check passed; mypy reported no issues.
