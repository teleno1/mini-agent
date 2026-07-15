# 04 - Assemble trusted configuration, instructions, and Context Frames

**What to build:** Each model request receives a reproducible Context Frame assembled from strictly validated configuration and path-scoped project instructions without allowing lower-trust content to weaken host safety.

**Blocked by:** 03 - Persist, list, and resume text-only Sessions.

**Status:** completed

- [x] Implement strict built-in, user TOML, project TOML, environment, CLI, and Session-override precedence with per-field provenance and unknown-key failures.
- [x] Read the API Key only from the agreed environment variable, redact it everywhere, and prevent project configuration from setting credentials or Provider Base URL.
- [x] Provide `init` and `config show`; initialization requires confirmation before changing project configuration or ignore rules and never writes a secret.
- [x] Discover root and nested `AGENTS.md` by target path, enforce size/encoding/symlink boundaries, detect multi-target conflicts, and keep ordinary repository content untrusted.
- [x] Assemble typed Context Frame layers in the agreed authority order and map roles without lowering instruction authority.
- [x] Persist Session configuration changes and Context Manifests containing non-secret sources, hashes, token estimates, Summary Boundary, and included event ranges.
- [x] Resume reapplies current safety and instructions, and configuration tests cover precedence, safety ceilings, reset, and forbidden in-Session mutations.

## Completion evidence

- Configuration precedence, provenance, safety caps, unknown TOML keys/types, and CLI Base URL precedence: `tests/test_ticket04.py::test_configuration_precedence_provenance_caps_and_unknown_keys`.
- Project credentials/Base URL rejection, environment-only API Key, blank-key rejection, safe config views/hash, and credential-bearing Base URL rejection: `tests/test_ticket04.py::test_project_configuration_cannot_supply_credentials_or_base_url` and `tests/test_ticket04.py::test_api_key_is_environment_only_and_safe_views_redact_it`.
- Session override confirmation, allowlist, reset, and forbidden active-Session identity changes: `tests/test_ticket04.py::test_session_overrides_require_confirmation_support_reset_and_forbid_identity_changes` and `tests/test_ticket04.py::test_manifest_and_session_overrides_persist_and_resume`.
- Confirmed/cancelled `init`, safe project config/ignore rule creation, and redacted `config show`: `tests/test_ticket04.py::test_init_requires_confirmation_and_config_show_never_prints_api_key`.
- Root/nested instruction scope, untrusted ordinary repository content, Workspace boundary, size/encoding warnings, symlink fail-closed behavior, and multi-target conflicts: `tests/test_ticket04.py::test_instruction_scope_boundary_and_untrusted_repository_content`, `tests/test_ticket04.py::test_instruction_size_encoding_symlink_and_multi_target_conflict`, and `tests/test_ticket04.py::test_instruction_target_symlink_check_is_fail_closed`. The real filesystem symlink case is platform-skipped when Windows symlink creation is unavailable; the target boundary branch is exercised without that capability.
- Authority-ordered typed Context Frame, role preservation/downgrade for history, token reserve failure, Manifest source/hash/token metadata, Summary Boundary, event range, and secret-free provenance: `tests/test_ticket04.py::test_context_frame_authority_order_manifest_and_budget`.
- Durable Context Manifest ordering before the model request, durable Session overrides, current instruction hash change notice, current safety configuration on Resume, and reset: `tests/test_ticket04.py::test_manifest_and_session_overrides_persist_and_resume`.
- Full verification: `uv run pytest` (27 collected: 26 passed, 1 platform-capability skip), `uv run ruff check src tests`, `uv run mypy`, and `git diff --check`.
