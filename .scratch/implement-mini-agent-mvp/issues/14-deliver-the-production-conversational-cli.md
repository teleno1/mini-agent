# 14 - Deliver the production conversational CLI

**What to build:** Users receive the validated production interaction: a compact conversational terminal that streams useful activity, asks only actionable questions, and makes Plans, failures, compaction, completion, Session management, and recovery clear without exposing the prototype's diagnostic dashboard.

**Blocked by:** 09 - Complete bounded serial multi-Tool coding Turns; 11 - Compact long Sessions without losing observable state; 12 - Cancel, retry, and diagnose failed Turns safely; 13 - Reconcile interrupted work on Resume.

**Status:** completed

- [x] Render user input, streamed Agent text, and concise Tool activity while hiding internal Phase, diagnostic Actions, and ordinary context percentages.
- [x] Use transient status for thinking/reading, update Plans in place only for complex tasks, and collapse completed activity instead of accumulating noise.
- [x] Show focused permission blocks with normalized operation, affected resources, reason, and only valid choices; collapse decisions into concise audit lines.
- [x] Present incomplete output, retry progress, instruction changes, uncertain side effects, compaction, and recovery choices prominently and honestly.
- [x] Implement dedicated `init`, `sessions`, `resume`, `config show`, and `doctor` views plus the agreed startup options and in-Session configuration commands.
- [x] Completion reports outcome, verification, changed files, unresolved work, and next action; a failed Turn normally returns to interactive input.
- [x] Non-interactive terminals use stable plain text, no dynamic color, and safe denial when confirmation input is unavailable.
- [x] Semantic CLI integration tests cover task entry, text/tool streaming, allow/deny, Plan, compaction, cancellation, errors, completion, list, and Resume without broad ANSI snapshots.

## Completion evidence

- Conversational transcript and production rendering: `src/mini_agent/cli/presentation.py::ConversationPresenter` renders user input, streamed Agent text, transient Thinking/Reading status, terminal Tool audit lines, and factual completion fields without Phase, Actions, or ordinary context percentages. `tests/test_ticket14.py::test_production_cli_streams_text_and_reports_completion_without_dashboard` and `test_cli_streams_tool_activity_and_only_prompts_for_a_write` provide semantic assertions.
- Plans, permissions, and activity collapse: `AgentTurnApplication` now exposes presentation-only lifecycle observations for model retries, compaction, Plans, and Tool decisions; `TerminalPermissionInteraction` prints only operation/resources/reason and the four valid choices. `tests/test_ticket14.py::test_cli_renders_complex_plan_and_compaction_as_semantic_lines` and the write-denial assertions verify the visible contract.
- Honest failures, cancellation, retry, and recovery: `ConversationPresenter.mark_incomplete` labels cancelled partial output, `model.request.retrying` reports bounded retry progress, and the Resume command exposes inspect/abandon/retry/exit with instruction-change and uncertain-side-effect messaging. `tests/test_ticket14.py::test_cli_shows_provider_retry_progress`, `test_cli_acknowledges_cancellation_without_reporting_completion`, `test_cli_reports_redacted_provider_error_and_keeps_session_listable`, and `test_cli_resume_exposes_inspect_exit_and_abandon_choices` cover these paths; preceding ticket 12/13 suites cover durable recovery and forced-interrupt invariants.
- Command/configuration surface: `src/mini_agent/cli/app.py` implements `init`, `sessions`, `resume`, `config show`, `doctor`, `--workspace`, `--model`, `--base-url`, `--permission-mode`, `--help`, `--version`, and `/config show|set|reset`. `tests/test_ticket14.py::test_cli_exposes_init_and_config_views_without_credentials` verifies initialization and provenance-safe configuration output; existing `tests/test_cli.py` verifies listing and Resume.
- Non-interactive safety and integration quality: output uses the plain sink with no dynamic color, confirmations fail closed on unavailable input, and tests use semantic strings rather than ANSI snapshots. `uv run --frozen pytest -q` => `132 passed, 2 skipped`; `ruff format --check .`, `ruff check .`, `mypy`, `uv build`, and `git diff --check` all passed.
