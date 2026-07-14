# Claude Code public architecture research

Research date: 2026-07-12
Scope: architecture and user-visible behavior relevant to a small Python coding agent. This is not a compatibility specification.

## Evidence policy

This note separates three kinds of evidence:

- **Official**: Anthropic documentation, repositories, or release notes. These establish supported behavior, but usually not private implementation details.
- **Artifact observation**: a technical analyst's report of what they observed in a distributed bundle or accidentally published source artifact. The main source is Sathwick's [Reverse-Engineering Claude Code](https://sathwick.xyz/blog/claude-code.html). These observations are useful design clues, not a stable API. This note does not reproduce proprietary source.
- **Inference**: a design conclusion for Mini Agent. It may be inspired by the two categories above, but is our decision rather than a claim about Claude Code.

The reverse-engineering article is a point-in-time analysis dated 2026-03-31. Its exact counts, internal names, and feature-flagged systems can drift or may never become supported product features. Where official documentation now covers the same behavior, the official account takes precedence.

## Executive conclusion

Claude Code's transferable core is small: a conversational model repeatedly gathers context, invokes typed tools, observes results, changes the workspace, and verifies the outcome. The production product's complexity is primarily in the harness around that loop: permission enforcement, context lifecycle, persistence and recovery, responsive terminal interaction, extension loading, and failure handling. Anthropic now describes that loop explicitly as **gather context → take action → verify**, with every tool result fed back into the next decision ([official architecture guide](https://code.claude.com/docs/en/how-claude-code-works)).

For Mini Agent, adopt that core and a narrow version of the reliability envelope. Do not imitate Claude Code's terminal renderer, large command/tool catalog, provider-specific optimizations, extension ecosystem, subagents, or unreleased/flagged subsystems in the MVP.

## 1. The agent loop

### Verified behavior

**Official.** Claude Code is an agentic harness around a model. Anthropic describes a blended three-phase loop—context gathering, action, and verification—and says tool results feed the next model decision. Built-in capabilities cover file operations, search, command execution, web access, and optional code intelligence; orchestration tools include questions and subagents ([How Claude Code works](https://code.claude.com/docs/en/how-claude-code-works)). Users can interrupt a running action or queue steering input for the next action, so human input is part of the loop rather than merely its initial trigger.

**Artifact observation.** Sathwick describes a central query engine implemented as an asynchronous, streaming control flow which repeatedly assembles messages, calls the model, yields UI events, executes requested tools, and continues until a terminal response. The report emphasizes that tools share a generic contract and that errors become structured results rather than necessarily crashing the session ([Sathwick, sections 4, 5, and 14](https://sathwick.xyz/blog/claude-code.html#4-the-query-engine-brains-of-the-operation)). Exact internal function names and control-flow details are not independently verified here.

### Mini Agent decision

Adopt a provider-neutral `asyncio` loop with explicit states:

1. assemble durable instructions, recent history, plan, and new user input;
2. stream one model response;
3. if it contains tool calls, validate, authorize, execute, persist, and append results;
4. continue until a normal final response, cancellation, unrecoverable error, or configured step/budget limit;
5. make verification visible in the final report.

Keep orchestration deterministic. The model chooses *what* to do, while Python code owns state transitions, validation, permissions, persistence, cancellation, and limits. Run tools sequentially in the MVP. Parallel read-only execution is an optimization, not part of the core.

## 2. Tool system

### Verified behavior

**Official.** Anthropic documents tools as the mechanism that turns text reasoning into action. The primary categories are file operations, search, execution, web, and code intelligence ([How Claude Code works](https://code.claude.com/docs/en/how-claude-code-works)). Tool access can be controlled independently of instructions: prompts influence what the model attempts, while the harness enforces what is allowed ([permissions documentation](https://code.claude.com/docs/en/permissions)).

**Artifact observation.** Sathwick reports a uniform tool abstraction with a name/description, input schema, validation, permission check, execution path, and rendering behavior. The article also reports lazy/deferred discovery for some tools and stable tool ordering to reduce prompt-cache churn ([Sathwick, sections 5 and 25](https://sathwick.xyz/blog/claude-code.html#5-the-tool-system-60-tools-behind-a-single-interface)). The reported tool count and internal schema implementation are point-in-time details, not requirements.

### Mini Agent decision

Use a small `Tool` protocol with:

- stable name and concise description;
- Pydantic input model and JSON schema;
- risk metadata (`read`, `write`, `execute`, `dangerous`);
- async `execute(context, args) -> ToolResult`;
- structured success/error result with bounded model-facing text and optional artifact reference.

MVP tools should be only: `read_file`, `search_files`, `apply_patch`, `create_file`, and `shell`. Prefer `apply_patch` for reviewable incremental edits. A long result is stored in the session log while only a truncated, clearly marked view enters model context. Tool errors return to the model so it can recover; schema or permission failures never invoke the underlying operation.

## 3. Permissions and workspace safety

### Verified behavior

**Official.** Claude Code uses tiered permissions. Reads within configured working directories generally do not prompt; shell and file modifications normally do. Rules are `deny`, `ask`, or `allow`, evaluated in that order, and are enforced by the harness rather than by model instructions. Current documented modes include Manual/default, Accept Edits, Plan, Auto, `dontAsk`, and `bypassPermissions`; even the bypass mode retains a circuit breaker for root/home deletion and is recommended only in isolation ([Configure permissions](https://code.claude.com/docs/en/permissions)). Anthropic separately documents local file checkpoints as undo support; they do not reverse shell commands or external side effects ([Checkpointing](https://code.claude.com/docs/en/checkpointing)).

**Artifact observation.** Sathwick reports fail-closed defaults, layered command/path classification, command-prefix rules, and special handling for destructive patterns. It also reports classifier-assisted decisions in some modes ([Sathwick, section 6](https://sathwick.xyz/blog/claude-code.html#6-the-permission-system-safety-at-every-layer)). Classifier internals and the precise number of modes are version-sensitive and should not be treated as a contract.

### Mini Agent decision

Implement the already agreed three policies (`suggest`, `auto-edit`, `full-auto`) as deterministic host rules. Every request resolves to `allow`, `ask`, or `deny`; deny wins. Canonicalize filesystem paths before checking that they remain under the startup workspace. Block known credential targets and traversal/symlink escapes. Give shell executions a timeout, capture exit status, and log the decision and command.

Do not add an ML safety classifier in the MVP. It introduces another fallible policy layer and is unnecessary for a learning project. `full-auto` must not mean unrestricted host access: retain hard denials for workspace escape, credential reads, and catastrophic deletion. Explicitly state that file patches can be reversed from logs/checkpoints but arbitrary shell side effects cannot.

## 4. Instructions, context, and compaction

### Verified behavior

**Official.** A session context includes conversation history, file and command outputs, system instructions, `CLAUDE.md`, auto-memory, and loaded extensions. Claude Code first clears older tool outputs and then summarizes the conversation as the window fills. Persistent project rules belong in `CLAUDE.md`; users can invoke `/compact` with a focus, while repeated compaction thrashing terminates with an error rather than looping forever. Skills and some tool definitions are loaded progressively, and subagents isolate their own context ([How Claude Code works](https://code.claude.com/docs/en/how-claude-code-works), [memory documentation](https://code.claude.com/docs/en/memory)).

**Artifact observation.** Sathwick reports token-aware thresholds, multi-stage recovery for context overflow, prompt-cache stability measures, result pruning, and summary generation ([Sathwick, section 10](https://sathwick.xyz/blog/claude-code.html#10-context-management-fighting-the-token-limit)). Precise thresholds and recovery stages are implementation observations, not official guarantees.

### Mini Agent decision

Build a transparent two-stage policy:

1. cap individual tool/file outputs before insertion, preserving a log reference;
2. near a configurable threshold, summarize older turns into a structured state containing objective, decisions, files inspected/changed, commands and outcomes, unresolved problems, and next steps.

Always retain the system prompt, workspace `AGENTS.md`, current plan, latest summary, and recent turns. Expose `/compact` and record compaction as a session event. Set a maximum number of consecutive automatic compactions so oversized inputs fail clearly. Do not implement prompt caching, progressive tool discovery, auto-memory, or subagent contexts in the MVP.

## 5. Session persistence and recovery

### Verified behavior

**Official.** Claude Code writes messages, tool uses, and results as plaintext JSONL under `~/.claude/projects/`. Sessions can be resumed under the same ID or forked to a new ID. It snapshots files before editing for session-local rewind; sessions otherwise begin with fresh model context, with persistent instructions/memory loaded separately ([How Claude Code works](https://code.claude.com/docs/en/how-claude-code-works)).

**Artifact observation.** Sathwick describes append-oriented transcripts, history and file-based coordination with lock/retry behavior ([Sathwick, sections 12 and 25](https://sathwick.xyz/blog/claude-code.html#12-session-persistence-and-history)). Specific paths and multi-agent IPC are irrelevant to our MVP and may vary by release.

### Mini Agent decision

Use `.mini-agent/sessions/<id>/events.jsonl` as the source of truth, plus small metadata and latest-summary files. Append an event after each user message, model response, permission decision, tool start/result, compaction, cancellation, and terminal error. Recovery replays events into derived state; partially started tools are marked interrupted and never silently re-run. Add `.mini-agent/` to `.gitignore` by default.

Support list, resume-latest, and resume-by-ID only. Session forks, parallel-writer coordination, cloud sync, and persistent learned memory are out of scope. If reversible patches are retained, treat them as a convenience checkpoint—not a substitute for Git.

## 6. Terminal interaction and runtime

### Verified behavior

**Official.** Claude Code supports interactive terminal sessions, cancellation, queued steering, permission prompts, plan mode, session management, and slash commands. The same documented agent loop appears through multiple interfaces, but local terminal execution has access to the user's files, commands, and Git state ([How Claude Code works](https://code.claude.com/docs/en/how-claude-code-works), [Interactive mode](https://code.claude.com/docs/en/interactive-mode)).

**Artifact observation.** Sathwick reports a sophisticated React/Ink-derived terminal UI with a custom renderer, immutable state updates, double buffering, and performance optimizations ([Sathwick, sections 7 and 11](https://sathwick.xyz/blog/claude-code.html#7-terminal-ui-react-but-for-your-terminal)). These choices solve production UX scale and are not intrinsic to a coding agent.

### Mini Agent decision

Use Typer for entry points and Rich for streaming text, tool summaries, and confirmations. Keep UI events separate from domain events so the agent loop is testable without a terminal. `Ctrl+C` cancels the active model/tool task and preserves the session. Implement only `/help`, `/plan`, `/compact`, `/permissions`, and `/exit`.

Do not build a retained-mode TUI, custom renderer, Vim mode, voice, remote control, or IDE bridge. Cross-platform shell adapters should select PowerShell on Windows and a POSIX shell on macOS/Linux while presenting one structured execution result.

## 7. Model/provider boundary

### Evidence and limits

**Official.** Claude Code itself is designed around Claude models and Anthropic's product surfaces; the official architecture guide does not promise an OpenAI-compatible provider abstraction ([How Claude Code works](https://code.claude.com/docs/en/how-claude-code-works)).

**Artifact observation.** Reverse-engineering analyses describe internal separation between query orchestration and several Anthropic deployment transports. That is not evidence of a generic public provider API. Claims that Claude Code is provider-agnostic should therefore be treated as analyst interpretation, not supported behavior.

### Mini Agent decision

Our `ModelProvider` is an independent design seam. MVP implements one OpenAI-compatible async provider configured by `base_url`, `api_key`, and `model`. Normalize streaming text, tool-call deltas, token usage, stop reasons, and provider errors into internal events. Do not leak provider-specific message objects into the loop or persistence schema.

## 8. Reliability patterns worth copying

These recommendations combine official behavior with artifact observations; they are design inferences, not claims of compatibility.

- **Persist before the next irreversible step.** Append messages, permissions, and tool results promptly so interruption loses little work.
- **Return operational errors to the loop.** Timeouts, non-zero exits, invalid patches, and missing files should be bounded tool results when recovery is possible.
- **Fail closed.** An unknown tool, invalid schema, undecidable path, or unknown permission mode is denied.
- **Bound every untrusted dimension.** Tool output size, execution time, model iterations, context-compaction retries, patch size, and stored event size need limits.
- **Keep state derivable.** The append-only event log is authoritative; UI state and current conversation state are projections.
- **Verify explicitly.** A coding task is not complete merely because a patch applied. The loop should run relevant checks or say why it could not.
- **Separate user intent from authority.** A prompt may request an action, but the host policy alone grants it.

## 9. What to adopt, simplify, and exclude

| Area | MVP choice | Rationale |
| --- | --- | --- |
| Agent loop | **Adopt** async model → tool → observation loop with verification | The defining, officially documented architecture |
| Typed tools | **Adopt** one small uniform protocol | Enables validation, testing, and policy enforcement |
| File/search/shell | **Adopt** minimal set | Covers the coding feedback loop |
| Permission engine | **Adopt, simplify** deterministic three-mode policy | Preserves host-enforced safety without classifier complexity |
| Workspace boundary | **Adopt** canonical path checks and hard denials | Essential for local execution |
| Incremental edits | **Adopt** patch-first edits | Auditable and easier to recover |
| Context management | **Adopt, simplify** truncation plus structured summary | Necessary for long sessions; transparent enough to learn from |
| JSONL sessions | **Adopt** append-only events and replay | Debuggable, durable, and officially aligned at the behavioral level |
| Checkpoints | **Simplify** retain reversible file patches where practical | Useful, but Git remains the durable safety net |
| Interactive steering | **Adopt later within MVP** cancellation first; queued input if simple | Good UX, but cancellation is the essential primitive |
| Rich TUI internals | **Exclude** | Large engineering surface unrelated to agent fundamentals |
| MCP, hooks, plugins, skills | **Exclude** | Extension layers sit above the core loop |
| Subagents/worktrees | **Exclude** | Adds scheduling, isolation, and coordination concerns |
| Web and code intelligence | **Exclude** | Not required to exercise local coding loop |
| ML safety classifier | **Exclude** | Hard to validate and unnecessary for deterministic MVP policy |
| Prompt-cache optimization | **Exclude** | Provider-specific optimization |
| Auto-memory | **Exclude** | Separate product problem from resumable sessions |
| Unreleased flagged systems in analyses | **Exclude** | Unverified, unstable, and irrelevant to agreed scope |

## 10. Proposed component seams

The research supports the following provisional module boundaries for later tickets:

```text
CLI / Rich view
    ↓ UI commands and rendered events
Application session controller
    ↓
Agent loop ── ModelProvider
    │
    ├── Context manager / compactor
    ├── Tool registry ── Tool implementations
    ├── Permission policy ── User confirmation port
    └── Event store (JSONL) / session replay
```

The loop consumes interfaces; filesystem, subprocess, clock, model, confirmation UI, and event storage are injected ports. This makes a fake model and temporary workspace sufficient for integration tests.

## 11. Questions deliberately left to later tickets

- Exact internal message/event schema and streaming state machine.
- Command parsing and platform-specific shell containment details.
- Patch grammar and checkpoint representation.
- Context token estimation for OpenAI-compatible providers that do not report counts consistently.
- Configuration precedence and secrets handling.
- Packaging and release method.

## Sources and confidence

Primary sources:

- Anthropic, [How Claude Code works](https://code.claude.com/docs/en/how-claude-code-works) — high confidence for the public loop, tools, sessions, context, checkpoints, and interaction behavior.
- Anthropic, [Configure permissions](https://code.claude.com/docs/en/permissions) — high confidence for current public permission rules and modes.
- Anthropic, [How Claude remembers your project](https://code.claude.com/docs/en/memory) — high confidence for supported instruction and memory behavior.
- Anthropic, [Interactive mode](https://code.claude.com/docs/en/interactive-mode) — high confidence for user-visible terminal controls.
- Anthropic, [Checkpointing](https://code.claude.com/docs/en/checkpointing) — high confidence for file-only rewind boundaries.
- Anthropic, [Claude Code repository and changelog](https://github.com/anthropics/claude-code) — high confidence for product positioning and release-level behavior; it is not a complete open-source implementation specification.

Secondary artifact analysis:

- Sathwick, [Reverse-Engineering Claude Code: A Deep Dive into Anthropic's AI-Powered CLI](https://sathwick.xyz/blog/claude-code.html) — medium confidence for point-in-time structural observations, low confidence for longevity, exact counts, and feature-flagged/unreleased behavior. The article says it is based on examined source; this research did not independently inspect or preserve the proprietary artifact.

No proprietary source was copied into this document. All implementation recommendations are clean-room architectural conclusions expressed at the level of common agent patterns and publicly documented behavior.
