# 08 - Run bounded Shell commands under Permission Policy

**What to build:** The Agent can run recognized local development commands with cross-platform time/output limits while unknown or risky commands require an explainable user decision and credentials remain unavailable to the child process.

**Blocked by:** 04 - Assemble trusted configuration, instructions, and Context Frames; 06 - Let the Fake-driven Agent read and search a confined Workspace.

**Status:** completed

- [x] Run PowerShell on Windows and a POSIX shell on macOS/Linux from a validated Workspace-relative working directory.
- [x] Filter Provider credentials and unrelated sensitive environment values; reject model-provided environment overrides, interactive programs, and detached/background jobs.
- [x] Enforce the agreed default and maximum timeouts, bounded stdout/stderr, exit-code/duration results, and process-group lifecycle.
- [x] Implement exact explainable classification for recognized local read/build/test commands and ask for chaining, redirection, interpreters, network, installs, Git writes, deletion, and unknown executables.
- [x] Enforce suggest, auto-edit, and full-auto behavior without presenting full-auto as unrestricted host access.
- [x] Apply cooperative interrupt then platform-specific escalation; close output readers with the process and classify uncertain termination as interrupted.
- [x] Contract and integration tests cover quoting ambiguity, working-directory bounds, environment filtering, output truncation, timeout, cancellation, process trees, and permission audit.

## Completion evidence

- Cross-platform Shell execution, Workspace-relative working-directory validation, PowerShell/POSIX argv, bounded output, exit code, duration, timeout, and cancellation: `src/mini_agent/tools/shell.py`; `tests/test_ticket08.py::test_shell_runs_in_validated_directory_and_bounds_output`, `test_shell_reports_exit_code_and_filters_child_environment`, and `test_shell_timeout_terminates_process_group_and_closes_readers`.
- Credential/environment policy, rejected model environment overrides, interactive/detached rejection, sensitive target hard-deny, and explicit executable-path rejection: `filtered_child_environment`, `ShellInput`, and `classify_shell_command`; `tests/test_ticket08.py::test_shell_input_rejects_environment_overrides_and_caps_limits`, `test_child_environment_filters_provider_and_credential_values`, `test_shell_rejects_interactive_and_out_of_bound_working_directories`, and `test_shell_classifier_is_exact_and_explainable`.
- Explainable command classification and permission modes: `ShellCommandClassification` plus `PermissionPolicyGate`'s `full-auto-recognized-local` rule; `tests/test_ticket08.py::test_permission_modes_limit_full_auto_to_recognized_local_shell` and `test_fake_agent_records_full_auto_shell_permission_and_lifecycle`.
- Process-group lifecycle and uncertain termination: POSIX process groups, Windows CTRL_BREAK/taskkill escalation, Windows kill-on-close Job Objects, reader cleanup, and `ToolOutcome.INTERRUPTED`; `tests/test_ticket08.py::test_shell_cancellation_cooperatively_interrupts_process_group`, `test_shell_cancellation_reaches_a_nested_process_group`, and `test_uncertain_shell_termination_is_interrupted`.
- Full verification: `uv run --frozen pytest -q` (final run), `uv run --frozen ruff format --check .`, `uv run --frozen ruff check .`, `uv run --frozen mypy`, and `git diff --cached --check`.
