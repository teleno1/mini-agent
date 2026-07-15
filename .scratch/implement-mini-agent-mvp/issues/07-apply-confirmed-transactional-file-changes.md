# 07 - Apply confirmed transactional file changes

**What to build:** The Agent can propose exact, reviewable file additions and edits, obtain the required permission, apply them transactionally, and report or roll back known failures without escaping the Workspace.

**Blocked by:** 04 - Assemble trusted configuration, instructions, and Context Frames; 06 - Let the Fake-driven Agent read and search a confined Workspace.

**Status:** completed

- [x] Implement exact Add, Update, and Delete patch operations with no fuzzy application, at most 10 files and 256 KiB per call.
- [x] Implement single-file creation with UTF-8 bounds, optional parent creation, and strict no-overwrite behavior.
- [x] Reject binary, sensitive, external-link, reparse, and path-race cases; recheck approved targets and argument hashes immediately before commit.
- [x] Prepare same-filesystem temporary content, create Checkpoints, atomically replace targets, and roll back ordinary partial failures with explicit evidence.
- [x] Implement suggest and auto-edit write behavior, always asking for delete and Protected Path writes through a focused confirmation interaction.
- [x] Support allow once and exact-for-Session grants; any Tool, target, command, working-directory, or argument change invalidates the grant.
- [x] Persist redacted Permission Decisions and normalize success, invalid, denied, failed, cancelled, and interrupted Tool Result outcomes into the agreed terminal events.
- [x] Tests cover multi-file success, validation-before-write, rollback, create collision, protected resources, grant matching, and simulated interrupted commits.

## Completion evidence

- Exact bounded Patch/Create contracts and transaction implementation: `src/mini_agent/tools/patches.py` (`ApplyPatchInput`, `PatchOperation`, `CreateFileInput`, `ApplyPatchTool`, `CreateFileTool`) enforce exact context, UTF-8/binary checks, 10-file and 256-KiB bounds, no-overwrite Add/Create, and no implicit parent creation for `apply_patch`.
- Workspace safety and authorization rechecks: `src/mini_agent/tools/workspace.py` provides write-target normalization, sensitive/binary/link/reparse/protected-path rejection, same-path rechecks, and path/file-identity/hash checks immediately before commit; `src/mini_agent/application/agent.py` rechecks immutable argument hashes and final normalized resources.
- Transactional evidence and recovery of ordinary failures: `PatchCheckpoint` writes durable before-images and manifests, prepares same-directory temporary content, installs targets atomically/exclusively, and records rollback evidence or `interrupted` when rollback cannot be proven.
- Permission policy and audit: `src/mini_agent/application/permissions.py` implements suggest/auto-edit/full-auto defaults, focused confirmation, allow-once, exact Session grants bound to Tool/resources/argument hash, and mandatory confirmation for delete/Protected Path writes. `tool.validated.permission` persists redacted scope, rule, reason, resource summary, argument hash, and timestamp.
- Terminal normalization: `AgentTurnApplication` persists `tool.completed`, `tool.failed`, or `tool.interrupted` according to the structured `ToolOutcome`, including cancellation and rollback evidence, while preserving one terminal result per Tool Call.
- Ticket tests: `tests/test_ticket07.py` covers multi-file success and Checkpoints, validation-before-write, partial rollback, create collision/parent creation, permission modes and exact grants, Protected Paths, simulated interrupted commits, and Fake Agent permission/terminal events.
- Verification: `uv run --frozen pytest -q` => 75 passed, 2 skipped; `uv run --frozen ruff check src tests`, `uv run --frozen mypy`, and `git diff --check` pass.
