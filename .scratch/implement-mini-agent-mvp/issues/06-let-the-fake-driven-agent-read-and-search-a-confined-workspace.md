# 06 - Let the Fake-driven Agent read and search a confined Workspace

**What to build:** A Fake-driven coding Turn can request bounded file reads and repository searches while the host confines every target to the selected Workspace and records a complete Tool lifecycle.

**Blocked by:** 03 - Persist, list, and resume text-only Sessions.

**Status:** completed

- [x] Define the shared typed Tool, Risk Assessment, Tool Call, Tool Result, registry, and lifecycle contracts without UI or permission prompting inside Tools.
- [x] Resolve one real Workspace root and reject absolute, drive-changing, UNC, traversal, device, binary, sensitive, and out-of-bound targets with platform-correct comparisons.
- [x] Permit reads through links only when the resolved target remains inside the Workspace; never reveal sensitive content through denial details.
- [x] `read_file` implements UTF-8/BOM handling, line/range continuation, and the agreed 500-line/64-KiB limits.
- [x] `search_files` supports literal/regex, directory, and glob; uses `rg` without a Shell when present, safely falls back to Python, and honors result limits and ignored targets.
- [x] Safe reads/searches are automatically authorized and their proposed, validated, started, and terminal events yield exactly one Tool Result per persisted Tool Call.
- [x] Unit, contract, and temporary-Workspace integration tests cover Windows/POSIX path cases, symlinks/reparse behavior, truncation, and Fake-driven model adaptation to failures.

## Completion evidence

- Shared contracts and lifecycle: `src/mini_agent/tools/contracts.py`, `src/mini_agent/application/ports.py`, `src/mini_agent/domain/sessions.py`, and `src/mini_agent/application/agent.py` define typed Tool/Risk/Call/Result/Registry/Permission boundaries, durable proposed/validated/started/completed/failed/interrupted states, redacted permission records, normalized-call authorization, final path/argument-hash rechecks, active-time budgeting, and no Tool-level UI or prompting.
- Workspace confinement: `tests/test_tools.py::test_workspace_rejects_absolute_drive_unc_traversal_and_device_paths`, `test_workspace_allows_internal_link_but_rejects_external_link`, `test_workspace_confines_windows_directory_reparse_points`, `test_workspace_denies_sensitive_and_binary_targets_but_allows_env_template`, and `test_search_rg_filters_sensitive_targets_before_returning_matches` cover POSIX/Windows lexical forms, resolved links and junctions, sensitive/binary targets, and bounded denial details.
- Bounded reads: `tests/test_tools.py::test_read_file_handles_bom_ranges_and_line_continuation` covers UTF-8 BOM removal, line ranges, byte continuation, 500-line and 64-KiB bounds.
- Bounded searches: `tests/test_tools.py::test_search_python_fallback_supports_aliases_regex_glob_ignore_and_columns` and `test_search_rg_is_direct_and_result_is_bounded` cover literal/regex, directory/glob aliases, ignored files and negation, literal columns, Python fallback, direct `rg`, result count, and byte limits.
- Agent lifecycle and adaptation: `tests/test_agent_tools.py` covers serial Fake-driven read/search, bounded traversal denial before `tool.started`, immutable normalized calls reaching the Permission Gate, final path/argument-hash rechecks, active-budget failure, invalid arguments, automatic read authorization, non-read denial without execution, persisted authorization metadata, one terminal result per Tool Call, ContextFrame Tool pairing/current-user layering, and interrupted execution.
- Verification on Windows/Python 3.12 after code review: `uv run --frozen pytest` => 55 passed, 2 skipped (temporary symlink capability unavailable); `uv run --frozen ruff format --check .`, `uv run --frozen ruff check .`, and `uv run --frozen mypy` passed; artifact build/audit and wheel/source-distribution install smoke passed; `git diff --check` passed. No push performed.
