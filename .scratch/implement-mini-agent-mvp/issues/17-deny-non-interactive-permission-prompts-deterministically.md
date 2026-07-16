# 17 - Deny non-interactive permission prompts deterministically

**What to build:** A Coding Agent running without an interactive terminal fails closed whenever a Tool Call requires confirmation. Piped or redirected input cannot grant authority, while an interactive user retains the focused allow-once, allow-exact-for-Session, deny, and cancel choices.

**Blocked by:** None - can start immediately.

**Status:** completed

- [x] User Interaction receives or derives a trustworthy terminal-interactivity capability before presenting a permission decision.
- [x] Every confirmation-required write, Protected Path operation, and Shell call is denied without prompting when terminal input is non-interactive.
- [x] Piped affirmative values such as `allow` or `session` cannot authorize a Tool Call in non-interactive execution.
- [x] The denial is persisted as a redacted Permission Decision linked to the exact Tool Call and the Agent Loop receives the corresponding denied Tool Result.
- [x] Interactive terminals retain only the four valid focused choices and exact Session grants retain their existing argument-hash semantics.
- [x] Semantic integration tests cover EOF, redirected/piped affirmative input, non-interactive one-shot execution, and interactive allow/deny behavior without broad ANSI snapshots.

## Completion evidence

- `test_noninteractive_terminal_interaction_denies_before_prompt` and `test_confirmation_without_a_terminal_capability_fails_closed` verify that a missing or false `is_interactive` capability records `non-interactive-input` and never calls the prompt.
- `test_noninteractive_confirmation_required_operations_are_denied_without_prompt` covers `create_file`, Protected Path `apply_patch` on `AGENTS.md`, and confirmation-gated `shell`; all assert no prompt, no `tool.started`, and denied lifecycle output.
- `test_noninteractive_one_shot_eof_or_piped_affirmative_denies_without_prompt` covers EOF plus redirected `allow` and `session` input. It asserts the file is unchanged and the CLI emits no permission prompt.
- The same test asserts the persisted `tool.validated.permission` has the exact Tool Call ID, `decision=deny`, `matched_rule=non-interactive-input`, normalized resources, and no file content; the denied `tool.failed` has the same Tool Call ID, `causation_id` equal to the validation event ID, `outcome=denied`, and `error.code=non-interactive-permission`.
- The Provider request assertion confirms the Agent Loop receives a `ToolResultMessage` with `outcome=denied`.
- `test_interactive_confirmation_accepts_the_four_focused_choices`, `test_interactive_confirmation_rejects_unlisted_affirmative_alias`, and `test_interactive_terminal_retains_allow_once_and_denies_focus_choices` cover the focused interactive choices and terminal behavior without ANSI snapshots.
- `test_interactive_session_grant_requires_an_exact_argument_hash` and the existing `test_permission_modes_and_exact_session_grants` verify Session grants do not survive a changed argument/resource identity.
- Verification: `uv run ruff check src tests`, `uv run ruff format --check src tests`, `uv run mypy`, and `uv run pytest -q` all passed; final suite result was 154 passed, 2 skipped, 1 warning.
