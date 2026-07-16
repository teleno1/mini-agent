# 19 - Report only successful Shell verification

**What to build:** Completion Reports distinguish verification actually performed successfully from attempted commands that were denied, failed, timed out, cancelled, or left uncertain. Users receive an honest outcome, unresolved work, and relevant next action without failed commands being presented as verification evidence.

**Blocked by:** None - can start immediately.

**Status:** completed

- [x] A Shell command appears in Completion Report verification only when its Tool Result outcome is successful.
- [x] Denied, invalid, failed, timed-out, cancelled, and interrupted Shell attempts are excluded from verification and represented in unresolved work with their observable outcome.
- [x] When no successful verification exists, the report states that verification is unavailable and recommends the relevant safe next action.
- [x] Mixed journeys report successful verification separately from unsuccessful attempts without losing either observation.
- [x] Changed files and overall outcome do not imply verified success when every attempted verification command was unsuccessful.
- [x] Domain/application and semantic CLI tests cover successful, denied, failed, timed-out, cancelled, interrupted, and mixed Shell results.

## Completion evidence

Record concrete test names and rendered Completion Report assertions here before marking this ticket completed.

- `build_completion_report()` now adds Shell commands to `verification` only for `ToolOutcome.SUCCESS`; every other Shell outcome is keyed by Tool Call ID and retained in `unresolved_work` with the command, outcome, and error code. No-success reports use `verification=("unavailable",)` and recommend reviewing observations before safely rerunning the relevant verification command.
- `tests/test_ticket19.py::test_completion_report_only_verifies_successful_shell_results` covers successful, denied, invalid, failed, timed-out (`interrupted` + `timeout`), cancelled, and interrupted outcomes; it asserts one successful verification and six unresolved observations.
- `tests/test_ticket19.py::test_completion_report_preserves_a_failed_attempt_after_a_successful_retry` and `tests/test_ticket09.py::test_ticket09_fake_turn_orders_read_edit_test_denial_replan_and_report` cover mixed application journeys: the successful `pytest -q` appears once in verification while the failed attempt remains unresolved.
- `tests/test_ticket19.py::test_completion_report_marks_verification_unavailable_without_success` and `test_cli_renders_unavailable_verification_and_unsuccessful_shell_observation` assert `Verification: unavailable`, the observable timeout outcome, the changed file, and the safe next action. `test_cli_renders_successful_verification_separately_from_failed_retry` asserts the CLI renders successful verification and failed retry evidence on separate report fields.
- Final verification: `uv run --frozen pytest -q` => `162 passed, 2 skipped`; `uv run --frozen ruff format --check src tests`; `uv run --frozen ruff check src tests`; `uv run --frozen mypy`; and `git diff --check` all passed.
