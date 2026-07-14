# Research Claude Code's public architecture and feature boundaries

Type: research
Status: resolved
Blocked by: none

## Question

Which publicly documented Claude Code architecture concepts and user-visible behaviors are relevant to this learning project, which details are verified versus inferred, and which ideas should the Mini Agent deliberately simplify or exclude?

The resolution must link a Markdown research asset based on high-trust primary sources and distinguish source-backed facts from project-specific inference.

## Answer

Research asset: [Claude Code public architecture research](../../../docs/research/claude-code-public-architecture.md).

Claude Code's transferable core is a small agentic loop—gather context, act through typed tools, observe results, and verify—surrounded by a production reliability envelope for permissions, context lifecycle, persistence, recovery, and terminal interaction. Anthropic's [official architecture guide](https://code.claude.com/docs/en/how-claude-code-works) verifies that behavioral core; its [permissions documentation](https://code.claude.com/docs/en/permissions) verifies host-enforced authorization modes and rules. Sathwick's [reverse-engineering analysis](https://sathwick.xyz/blog/claude-code.html) provides useful implementation clues about a streaming query engine, uniform tool contracts, compaction stages, and append-oriented sessions, but its internal names, counts, and flagged features are point-in-time artifact observations rather than stable contracts.

For Mini Agent, adopt the asynchronous model → tool → observation loop, a small typed tool protocol, deterministic host permissions, workspace confinement, bounded outputs, structured context summaries, append-only JSONL events, cancellation, and explicit verification. Simplify tool execution to serial order and the terminal to Typer/Rich. Exclude the custom renderer, large tool/command catalog, prompt-cache tricks, ML safety classifiers, auto-memory, extensions, subagents, and unreleased systems. Keep the `ModelProvider` seam as an independent Mini Agent design rather than claiming Claude Code is provider-neutral.
