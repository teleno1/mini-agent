# 10 - Store and reread large Tool Results as immutable Artifacts

**What to build:** Large Tool output remains available as an integrity-checked Session Artifact while the model receives only a bounded preview and can request controlled ranges when more evidence is needed.

**Blocked by:** 03 - Persist, list, and resume text-only Sessions; 06 - Let the Fake-driven Agent read and search a confined Workspace.

**Status:** completed

- [x] Crossing the configurable 32-KiB persistence threshold atomically writes an immutable Artifact before the terminal Tool event references it.
- [x] References contain stable identity, Session-relative path, media type, byte count, SHA-256 digest, preview, and truncation state.
- [x] The model cannot choose Artifact paths and normal model file Tools remain unable to access `.mini-agent` directly.
- [x] A dedicated controlled Tool rereads bounded ranges without exceeding the absolute Tool response ceiling.
- [x] Redact known credentials and sensitive environment values before either inline persistence or Artifact writing, retaining no unredacted copy.
- [x] Failed Artifact writes cannot produce successful Tool Results; failed reference events leave detectable orphans without fabricating commitment.
- [x] Integration tests cover exact threshold edges, integrity verification, ranged reread, path confinement, redaction, immutability, and orphan detection.

## Completion evidence

- Threshold, atomic ordering, immutable read-only files, reference fields, SHA-256 verification, redaction, and tamper detection: `tests/test_ticket10.py::test_large_tool_result_is_redacted_artifact_with_integrity_checked_reference` and `test_artifact_threshold_is_strictly_greater_than_32_kib`; implementation in `src/mini_agent/adapters/artifacts.py`, `src/mini_agent/domain/artifacts.py`, and `src/mini_agent/application/agent.py`.
- Controlled identity-only reread, active-writer compatibility, range bounds, and the 64-KiB serialized response ceiling: `tests/test_ticket10.py::test_artifact_reader_uses_identity_not_model_path_and_bounds_ranges`, `test_agent_can_reread_an_artifact_while_its_session_writer_is_open`, and `test_tool_result_over_absolute_ceiling_is_failed_and_not_persisted`; implementation in `src/mini_agent/tools/artifacts.py` and `src/mini_agent/adapters/session_store.py`.
- Normal Workspace Tools continue to reject `.mini-agent` through the existing confinement/search coverage in `tests/test_tools.py`, while `read_artifact` resolves only Session-known identities; no model-supplied filesystem path is accepted by `ArtifactReadInput`.
- Credential redaction covers configured/environment API keys, common credential formats, and GitHub token forms before inline or Artifact persistence; `test_large_tool_result_is_redacted_artifact_with_integrity_checked_reference` asserts secrets are absent from both surfaces.
- Failed Artifact reference persistence cannot yield `tool.completed`, and uncommitted files are discoverable: `tests/test_ticket10.py::test_failed_artifact_reference_leaves_an_orphan_without_success` and `test_uncommitted_artifact_is_detectable_as_an_orphan`.
- Verification: `uv run --frozen pytest -q` (99 passed, 2 skipped), `uv run --frozen ruff format --check .`, `uv run --frozen ruff check .`, `uv run --frozen mypy`, and `git diff --check` all passed.
