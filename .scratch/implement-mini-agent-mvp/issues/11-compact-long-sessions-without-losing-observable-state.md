# 11 - Compact long Sessions without losing observable state

**What to build:** A long coding Session can continue near the model context limit by compacting redundant data and producing a validated factual summary while retaining the original durable history.

**Blocked by:** 04 - Assemble trusted configuration, instructions, and Context Frames; 09 - Complete bounded serial multi-Tool coding Turns; 10 - Store and reread large Tool Results as immutable Artifacts.

**Status:** completed

- [x] Estimate input tokens conservatively, calibrate with Provider usage, and reserve the agreed response capacity including the small-window cap.
- [x] Preserve safety policy, Permission Policy, active Tool Definitions, current user message, and unfinished Tool protocol pairs under all pressure.
- [x] Micro-compact Artifact-backed results, superseded Plans, and re-derivable operational state before requesting a model summary.
- [x] Generate the fixed structured Context Summary fields and validate types, evidence references, Artifact references, and monotonic Summary Boundary before activation.
- [x] Assemble later requests from the latest valid summary plus relevant events after its boundary without deleting original Session Events or claiming hidden reasoning was retained.
- [x] Record compaction lifecycle and fail the Turn clearly after three unsuccessful attempts instead of sending a known-oversized request.
- [x] Tests cover threshold edges, Tool pairing, old-summary recompression, invalid/hallucinated references, failure recovery, and preservation of objectives, constraints, changes, failures, and next actions.

## Completion evidence

- Conservative reserve calculation, small-window cap, Provider usage calibration, and calibrated pre-request budget checks: `src/mini_agent/domain/compaction.py::response_reserve_tokens`, `TokenEstimator`, `src/mini_agent/application/agent.py::AgentTurnApplication._ensure_context_budget`, and `tests/test_ticket11.py::test_response_reserve_edges_and_provider_calibration`.
- Safety/Permission/Tool Definition/current-user layers remain rebuilt by `ContextBuilder`; micro-compaction retains recent and unfinished Tool protocol pairs, with lifecycle reduction in `ContextCompactor.micro_compact_events`. Covered by `tests/test_ticket11.py::test_micro_compaction_keeps_tool_call_result_pair_and_artifact_identity` and the existing Context Frame pairing tests.
- Artifact-backed results retain a bounded, parseable Artifact identity and integrity fields; completed Plans and re-derivable Tool lifecycle noise are represented by the latest summary/state. Covered by the same micro-compaction test and `ContextCompactor._recompress_candidate`.
- Fixed summary fields, typed values, known event/Artifact references, boundary ceiling, and monotonic boundary validation: `ContextSummary.from_dict`; covered by `tests/test_ticket11.py::test_summary_validates_evidence_artifact_references_and_boundary` and `test_old_summary_recompression_keeps_facts_and_moves_boundary`.
- Latest summary and `summary_boundary` are projected from append-only `context.compacted` events; Resume selects messages/events after the boundary while original `events.jsonl` remains intact. Covered by `tests/test_ticket11.py::test_long_session_compacts_without_deleting_events`.
- `context.compaction.started`, `context.compacted`, and `context.compaction.failed` are durable lifecycle events; three unsuccessful attempts fail before `model.request.started`, while a transient summary failure retries and recovers. Covered by `tests/test_ticket11.py::test_three_unsuccessful_compactions_fail_before_provider_request` and `test_compaction_recovers_after_one_summary_generation_failure`.
- Objective, constraints, changed files, commands/results, failures, and next actions remain in the factual summary: `tests/test_ticket11.py::test_fact_summary_preserves_changes_commands_and_failures` and `test_long_session_compacts_without_deleting_events`.
- Full verification: `uv run --frozen pytest -q` (`107 passed, 2 skipped`), `uv run --frozen ruff format --check .`, `uv run --frozen ruff check .`, `uv run --frozen mypy`, and `git diff --check` all passed.
