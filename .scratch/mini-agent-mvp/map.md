# Mini Agent MVP Architecture Map

Label: `wayfinder:map`
Status: complete

## Destination

Produce an implementation-ready MVP specification and a recorded set of architecture decisions for a cross-platform Python coding agent inspired by Claude Code's public concepts. A later implementation effort should be able to begin without unresolved product or system-design questions.

## Notes

- This is a planning map. Do not implement the Mini Agent while resolving it.
- Use `grilling` and `domain-modeling` for design decisions; update `CONTEXT.md` whenever domain terminology is resolved.
- Use `research` when external documentation is required and link the resulting Markdown asset from the resolution.
- Use `prototype` for concrete CLI behavior that needs human feedback.
- Refer to maps and tickets by their linked names, not by bare numbers.
- GitHub Issues are the preferred tracker, but this effort uses local Markdown because `gh` was unavailable when the map was charted.
- Agreed baseline: Python 3.12+, asyncio, Typer, Rich, Pydantic, httpx, pytest, and pytest-asyncio; no Agent framework dependency.
- Agreed MVP: interactive CLI, one OpenAI-compatible provider behind a provider interface, file/search/patch/shell tools, `AGENTS.md`, permission modes, context compaction, JSONL sessions, visible plans, and final reports.
- Supported platforms: Windows, macOS, and Linux.

## Decisions so far

<!-- One linked gist per resolved ticket. The full answer lives in that ticket. -->

- [Research Claude Code's public architecture and feature boundaries](issues/01-research-claude-code-public-architecture.md) — Adopt the verified agent-loop core and a narrow reliability envelope; treat reverse-engineered internals as design clues, not compatibility requirements.
- [Design the Agent Loop and runtime state machine](issues/02-design-agent-loop-and-runtime-state-machine.md) — Use a bounded two-level state machine with provider-neutral messages, ephemeral streaming deltas, serial Tool Calls, durable transitions, interruption reconciliation, and deterministic test ports.
- [Design tool contracts, workspace confinement, and permissions](issues/03-design-tools-workspace-and-permissions.md) — Use narrow typed Tools, conservative cross-platform path checks, recoverable patch transactions, explainable Shell rules, and exact audited authorization under three bounded permission modes.

- [Design context management and the session event model](issues/04-design-context-and-session-event-model.md) - Use a single append-only JSONL authority, derived context projections, immutable Artifact references, validated structured summaries, bounded compaction, and conservative interruption-aware Resume.

- [Prototype the CLI interaction and confirmation flow](issues/05-prototype-the-cli-interaction.md) - Keep the validated interaction state machine, but present it as a minimal conversational CLI with transient status, contextual permission prompts, optional Plans, and dedicated Session views rather than a diagnostic dashboard.

- [Define project structure, tests, and MVP acceptance](issues/06-define-project-structure-tests-and-acceptance.md) - Use a single inward-dependent src package, explicit external ports, deterministic layered tests, cross-platform offline CI, and observable safety-and-recovery acceptance gates.

- [Design prompt assembly and configuration precedence](issues/07-design-prompt-assembly-and-configuration-precedence.md) - Build provenance-aware Context Frames with non-degradable instruction layers, path-scoped AGENTS.md, trust-classed configuration precedence, secret isolation, bounded context budgeting, and audited Provider role mapping.

- [Design streaming, failure classification, and recovery](issues/08-design-streaming-failures-and-recovery.md) - Normalize ephemeral streams, classify redacted Failures, bound retries and timeouts, acknowledge cancellation, stop on persistence uncertainty, and reconcile interrupted side effects from observable reality.

- [Design packaging, installation, versioning, and release workflow](issues/09-design-packaging-installation-versioning-and-release.md) - Package with Hatchling and uv as a Python 3.12+ typed wheel, deploy locally or from Git, keep runtime paths writable outside site-packages, and make public releases optional rather than an MVP gate.

- [Assemble and audit the implementation-ready MVP specification](issues/10-assemble-and-audit-the-implementation-ready-mvp-specification.md) - Consolidate the resolved architecture into a ready-for-agent specification with explicit implementation order, normalized Tool outcomes, cross-platform tests, and a complete acceptance matrix.

## Not yet specified

None. The destination is reached; production implementation is handed off separately.

## Out of scope

- Claude Code protocol or internal implementation compatibility.
- Multiple production model-provider implementations in the MVP.
- Sub-agents, MCP, Hooks, IDE integration, Web search, desktop UI, and Web UI.
- Container-grade sandboxing, cloud session sync, and branched sessions.
- Real-model calls in CI.
