# 09 - Complete bounded serial multi-Tool coding Turns

**What to build:** A user can ask a real configured model to inspect code, make a confirmed change, run verification, adapt to denial or recoverable Tool failure, and finish with an honest report in one bounded serial Agent Turn.

**Blocked by:** 05 - Stream a real OpenAI-compatible text response; 07 - Apply confirmed transactional file changes; 08 - Run bounded Shell commands under Permission Policy.

**Status:** completed

- [x] Orchestrate repeated Context Frame, model, Tool authorization/execution, Tool Result, and continuation cycles through Application Ports.
- [x] Execute multiple Tool Calls serially in model order, preserve provider-required pairing, and cancel not-yet-started calls only on user cancellation or host failure.
- [x] Treat invalid input, denial, and recoverable Tool failure as structured observations so the model can adjust rather than falsely completing.
- [x] Persist required transitions before the next possible side effect and stop immediately when durable state cannot advance.
- [x] Enforce one active Turn, one active model request or Tool, 25 model requests, 50 Tool Calls, 30 active minutes, token usage, output, and retry budgets.
- [x] Persist complete Plan snapshots with at most one in-progress step and omit Plans for simple work.
- [x] Complete only on a normal no-Tool stop and report outcome, verification performed or unavailable, changed files, unresolved work, and next action.
- [x] A deterministic end-to-end test proves read, edit, test, denial/replan, recoverable failure, and final report ordering through the Fake Provider.

## Completion evidence

- Application orchestration and fresh Context Frames: `src/mini_agent/application/agent.py::AgentTurnApplication.run` rebuilds a frame, starts one model request, closes the normalized response, authorizes and executes each Tool, appends the paired Tool Result, and continues until a normal no-Tool response. `tests/test_ticket09.py::test_ticket09_fake_turn_orders_read_edit_test_denial_replan_and_report` asserts six captured Context Frames and ordered observations.
- Serial Tool ordering and pairing: the `for block in response.message.tool_calls` loop awaits each call before proposing the next, while `ContextFrame` history retains the structured assistant call and `ToolResultMessage`. The ticket 09 journey and existing `tests/test_agent_tools.py` pairing assertions cover this.
- Structured observations: invalid, denied, failed, cancelled, and interrupted outcomes remain `ToolResultMessage` observations; the ticket 09 journey proves denial and a recoverable verification failure are followed by model-selected replans. Existing `tests/test_agent_tools.py` covers invalid input and interruption terminal normalization.
- Durable-before-side-effect ordering: `tool.proposed`, `tool.validated`, and `tool.started` are appended before execution; the terminal event is durable before the next Context Frame/model request. Existing `tests/test_agent_tools.py`, `tests/test_ticket07.py`, and `tests/test_ticket08.py` assert lifecycle ordering and no-start denial.
- Turn safety budgets: `TurnBudgets` caps model requests, Tool Calls, active seconds, token usage, output bytes, and retries; the application tracks one active Session Turn and one awaited model/Tool operation. `tests/test_ticket09.py` covers active-Turn rejection, Tool budget exhaustion before the second side effect, and bounded pre-output retry; existing tests cover active-time exhaustion.
- Plan snapshots: `src/mini_agent/domain/plans.py` validates complete immutable snapshots and at most one in-progress step; `plan.updated` is projected by `src/mini_agent/domain/sessions.py`. The ticket 09 journey verifies multiple snapshots, final completion, and no Plan for simple text work.
- Honest normal completion: `src/mini_agent/domain/reports.py::CompletionReport` records outcome, verification, changed files, unresolved work, and next action; `turn.completed` persists the report only after a normal no-Tool stop. Ticket 09 asserts changed files, verification commands, and persisted report equality.
- Deterministic Fake Provider end-to-end coverage: `tests/test_ticket09.py` covers read, denied edit, successful edit, failed then successful verification, final report ordering, simple no-Plan work, budgets, active Turn exclusivity, and recoverable model retry. Final verification: `uv run --frozen pytest -q` => `92 passed, 2 skipped`; `ruff format --check`, `ruff check`, `mypy`, and `git diff --check` passed.
