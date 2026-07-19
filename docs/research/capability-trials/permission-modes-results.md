# Permission-mode comparison trial results

Task: [Compare permission-mode behavior on key tasks](https://github.com/teleno1/mini-agent/issues/23).

## Conclusion

The three modes preserve the same hard safety boundary while changing the
confirmation surface as specified:

| Operation | suggest | auto-edit | full-auto |
| --- | --- | --- | --- |
| Ordinary source-file update | asks; non-interactive fallback denies | allow | allow |
| Recognized local test (`pytest -q`) | asks | asks | allow |
| Unknown Shell command | asks | asks | asks | 
| Sensitive target (`cat .env`) | hard deny | hard deny | hard deny |
| Protected Path write (`AGENTS.md`) | asks | asks | asks |

The direct policy matrix also exercised an exact Session grant: the first
normalized call was allowed, the identical repeat was allowed by the exact
grant, and a changed resource/argument hash prompted again and was denied.
The scripted interaction saw two prompts, proving that the changed call did
not inherit the previous grant.

## Evidence

- Regression command: `.venv\\Scripts\\python.exe -m pytest -q
  tests/test_ticket07.py tests/test_ticket08.py tests/test_ticket17.py
  tests/test_agent_tools.py tests/test_remediation_acceptance.py`
  — 50 passed.
- The permission matrix used the public `PermissionPolicyGate`,
  `ShellTool.assess`, `ShellInput`, normalized Tool Calls, and a scripted
  confirmation interaction; no private implementation methods were used.
- `tests/test_ticket07.py::test_permission_modes_and_exact_session_grants`
  verifies ordinary-write mode behavior, exact Session reuse, and changed
  resource invalidation.
- `tests/test_ticket08.py::test_permission_modes_limit_full_auto_to_recognized_local_shell`
  verifies the recognized-local full-auto boundary, confirmation fallback for
  other Shell commands, and hard denials for sensitive/deletion cases.
- `tests/test_ticket17.py::test_interactive_confirmation_accepts_the_four_numeric_choices`
  and `tests/test_ticket17.py::test_interactive_confirmation_rejects_words_and_aliases`
  verify the four-choice prompt and fail-closed input handling.
- `tests/test_ticket17.py::test_noninteractive_confirmation_required_operations_are_denied_without_prompt`
  verifies honest denial without a terminal prompt.
- Existing three-run DeepSeek capability cells for RL-01 and RL-02 provide
  repeated real-model source-task evidence: each valid auto-edit run passed
  its independent fixture oracle and stopped safely when non-interactive Shell
  verification was denied. Their raw evidence remains in the existing
  `rl01/` and `rl02/` run directories.

## Decision

The permission-mode boundary is sufficient for the map's Python capability
report. No policy change is required. The next report may classify mode
differences as confirmation/automation differences, not as different safety
ceilings, while retaining the exact-grant invalidation and non-interactive
fail-closed observations.
