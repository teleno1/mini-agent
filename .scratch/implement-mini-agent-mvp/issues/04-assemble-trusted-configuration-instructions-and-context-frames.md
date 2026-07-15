# 04 - Assemble trusted configuration, instructions, and Context Frames

**What to build:** Each model request receives a reproducible Context Frame assembled from strictly validated configuration and path-scoped project instructions without allowing lower-trust content to weaken host safety.

**Blocked by:** 03 - Persist, list, and resume text-only Sessions.

**Status:** in-progress

- [ ] Implement strict built-in, user TOML, project TOML, environment, CLI, and Session-override precedence with per-field provenance and unknown-key failures.
- [ ] Read the API Key only from the agreed environment variable, redact it everywhere, and prevent project configuration from setting credentials or Provider Base URL.
- [ ] Provide `init` and `config show`; initialization requires confirmation before changing project configuration or ignore rules and never writes a secret.
- [ ] Discover root and nested `AGENTS.md` by target path, enforce size/encoding/symlink boundaries, detect multi-target conflicts, and keep ordinary repository content untrusted.
- [ ] Assemble typed Context Frame layers in the agreed authority order and map roles without lowering instruction authority.
- [ ] Persist Session configuration changes and Context Manifests containing non-secret sources, hashes, token estimates, Summary Boundary, and included event ranges.
- [ ] Resume reapplies current safety and instructions, and configuration tests cover precedence, safety ceilings, reset, and forbidden in-Session mutations.
