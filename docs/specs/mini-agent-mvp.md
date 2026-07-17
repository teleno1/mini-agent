# Mini Agent MVP Specification

Status: implementation-ready
Label: ready-for-agent

## Problem Statement

A developer learning how coding agents work needs a small, inspectable Python project that demonstrates the core architecture of a Claude Code-like terminal agent without claiming internal compatibility or inheriting the scope of a production platform. A simple model-to-tool loop is not enough: the learning value depends on explicit boundaries for tool authority, durable sessions, context pressure, cancellation, recovery, and observable failure.

The project therefore needs one implementation-ready contract that reconciles the research, architecture decisions, interaction prototype, safety rules, persistence model, testing strategy, and packaging plan. An implementation agent should be able to build the MVP without inventing product behavior or silently weakening its invariants.

## Solution

Build an asynchronous, single-agent, cross-platform terminal application in Python 3.12+. The Coding Agent accepts a task, assembles a provenance-aware Context Frame, streams a response from one OpenAI-compatible Model Provider, validates and authorizes typed Tool Calls, executes them serially inside a fixed Workspace, records durable Session Events, and repeats until it can provide an honest final report or must wait, fail, or be cancelled.

The MVP provides safe file reading, repository search, transactional text changes, file creation, and bounded Shell execution. It supports three Permission Policies, path-scoped `AGENTS.md`, visible Plans for complex tasks, structured context compaction, JSONL Session persistence, immutable Artifacts, conservative Resume, and a minimal conversational CLI. Host code—not model compliance—enforces permissions, paths, limits, event ordering, and recovery.

The product is an independent learning project. It adopts publicly described agent-loop concepts and selected reliability patterns, but does not reproduce or promise compatibility with Claude Code internals.

## User Stories

1. As a learner, I want to inspect a small Agent Loop, so that I can understand how model requests, Tool Calls, observations, and completion fit together.
2. As a developer, I want to start Mini Agent in a repository and enter a coding task, so that I can practice agent-assisted work from the terminal.
3. As a user, I want model text to stream as it arrives, so that the CLI feels responsive during longer requests.
4. As a user, I want incomplete streamed text marked honestly after a failure, so that I do not mistake a partial response for a committed answer.
5. As a user, I want the Agent to read bounded portions of text files, so that it can inspect code without flooding context.
6. As a user, I want the Agent to search repository text, so that it can locate relevant code efficiently.
7. As a user, I want exact, reviewable text patches, so that edits fail safely when the file no longer matches expectations.
8. As a user, I want new-file creation to refuse overwrites, so that existing work is not silently replaced.
9. As a user, I want Shell commands to have bounded time and output, so that a model cannot hold the terminal indefinitely.
10. As a user, I want all model-selected paths confined to the Workspace, so that repository tasks cannot escape into the host filesystem.
11. As a user, I want sensitive targets and catastrophic operations denied locally, so that prompt compliance is not the security boundary.
12. As a user, I want to choose suggest, auto-edit, or full-auto mode, so that I can trade convenience for confirmation while retaining hard safety limits.
13. As a user, I want a focused permission preview before risky actions, so that I can see the exact operation, target, and risk reason.
14. As a user, I want an exact approval to remain valid for the current Session when I choose it, so that repeated identical safe work does not require redundant confirmation.
15. As a user, I want changed Tool arguments to require a new decision, so that an approval cannot be reused for a different operation.
16. As a repository maintainer, I want root and nested `AGENTS.md` instructions applied by path, so that project and directory conventions reach the Agent.
17. As a security-conscious user, I want repository content kept below system safety authority, so that prompt injection in ordinary files cannot expand privileges.
18. As a user, I want to explicitly enable Plan Mode before complex work is represented by a visible Plan, so that planning is available when I choose it without exposing hidden reasoning.
19. As a user, I want Plan Mode disabled by default and simple tasks to avoid unnecessary Plans, so that the interface stays lightweight.
20. As a user, I want large Tool Results stored as Artifacts with bounded previews, so that useful evidence survives without exhausting model context.
21. As a user, I want long Sessions compacted into structured summaries, so that the Agent can continue while preserving objectives, constraints, decisions, changes, failures, and next steps.
22. As a user, I want original Session Events retained after compaction, so that a generated summary never becomes the only historical record.
23. As a user, I want every Session to survive process exit, so that I can list and Resume previous work.
24. As a user, I want unfinished Tools marked interrupted after a crash, so that unknown side effects are never silently replayed.
25. As a user, I want to inspect, abandon, or retry interrupted work as a new call, so that recovery is evidence-based.
26. As a user, I want changed `AGENTS.md` instructions disclosed on Resume, so that continued behavior does not pretend the old context is unchanged.
27. As a user, I want Ctrl+C acknowledged quickly and scoped to the active Turn, so that I can regain control without losing the Session.
28. As a user, I want recoverable Provider failures retried within a visible budget, so that transient outages do not create infinite loops.
29. As a user, I want configuration and authentication failures explained without blind retries, so that I can correct the actual problem.
30. As a user, I want persistence failure to stop new side effects, so that the Agent never outruns its durable audit trail.
31. As a user, I want concise errors with correlation IDs, so that I can diagnose failures without exposing credentials.
32. As a user, I want configuration values and their winning sources inspectable, so that precedence is understandable.
33. As a user, I want the API Key accepted only from the environment, so that the learning project does not pretend TOML is a secret store.
34. As a user, I want `--help` and `--version` to work without credentials, so that installation can be diagnosed offline.
35. As a contributor, I want deterministic Fake Provider tests, so that CI can verify Agent behavior without real-model calls.
36. As a contributor, I want the same behavior tested on Windows, macOS, and Linux, so that path, process, and terminal assumptions remain portable.
37. As a maintainer, I want a standard wheel and source distribution, so that the project can be installed locally or from Git without public publication.
38. As a maintainer, I want public GitHub or PyPI release to remain optional, so that packaging practice does not become a deployment obligation.

## Implementation Decisions

### Scope and architecture

- Use Python 3.12+, asyncio, Typer, Rich, Pydantic, and httpx. Do not use an Agent framework.
- Keep one installable `mini_agent` package with modules for CLI presentation, application orchestration, pure domain rules, Provider adapters, Tools, Session persistence, context management, instructions, and configuration.
- Dependencies point inward: domain rules have no terminal, HTTP, filesystem, or Provider dependency; application use cases depend on narrow Application Ports; adapters implement those ports.
- Required ports are Model Provider, Tool Registry, Permission Gate, Session Store, Context Builder, Compactor, Instruction Loader, Clock, ID Generator, Workspace, and User Interaction. Do not create an interface for every class.
- The MVP has one active Turn per Session, one active model request or Tool execution per Turn, and serial Tool execution.

### Agent Loop and messages

- Session Status is durable and user-visible; Turn Phase is transient and never treated as a resumable coroutine checkpoint.
- Provider-neutral messages are User Message, Assistant Message with ordered text and Tool Call blocks, and Tool Result Message linked to exactly one Tool Call ID.
- System policy, project instructions, Plan, Context Summary, and selected history form a derived Context Frame rather than synthetic conversation messages.
- A Turn completes only after a protocol-valid assistant response with no Tool Calls and a normal Provider stop. The final report states outcome, verification, changed files, unresolved work, and relevant next action.
- Default Turn budgets are 25 model requests, 50 Tool Calls, and 30 minutes of active execution time, excluding time spent waiting for user input. All are user-configurable only within host safety caps. Provider-reported token usage is accumulated as the portable cost budget; no built-in currency-price table is required. Limit exhaustion is an explicit Failure.

### Streaming and Provider contract

- The Model Provider emits normalized response-start, text-delta, Tool Call start/argument-delta/completion, usage, response-completion, and response-failure Stream Events.
- The production MVP adapter targets the widely implemented OpenAI-compatible Chat Completions streaming contract with structured tools. It uses configurable Base URL and model, direct httpx transport, and a documented subset rather than an official SDK. Responses-style APIs and free-form Tool emulation are outside the MVP.
- Deltas are ephemeral. Persist only complete protocol-valid assistant content, complete Tool Calls, request starts, and failures.
- Unknown or illegally ordered Provider events are protocol Failures. A Provider without reliable system-level instructions and structured Tool Calls is incompatible with Agent mode.
- A broken stream does not create a formal assistant message. Rendered text remains visibly incomplete, partial Tool arguments are discarded, and retry begins from the last durable Context Frame.
- Use a bounded renderer queue with upstream backpressure and coalesced text refresh. Plain-text fallback must preserve the Agent Loop when Rich/ANSI rendering fails.

### Conversational CLI and Context Frame remediation contract

- Plan Mode is an explicit runtime Session setting named `plan_mode`, disabled by default. Complexity, model output, repository content, instructions, ordinary configuration, and environment values cannot enable it. A one-shot flag or interactive command may change it for the next operation through the existing Session override lifecycle.
- A Turn captures Plan Mode at its start. When enabled, the existing complexity heuristic remains a second gate; when disabled, the Turn creates no new Plan lifecycle event or Plan presentation. Existing `plan.updated` and `plan.reset` event names, payloads, schema version, Resume behavior, and historical snapshots remain unchanged.
- The terminal permission adapter presents one stable numeric menu: `1` allow once, `2` allow the exact normalized call for the Session, `3` deny, and `4` cancel. It translates only these digits into the existing semantic confirmation values. Invalid input re-prompts without a decision; EOF, abort, I/O failure, and non-interactive input deny without prompting. Permission Policy rules, exact-grant scope, argument-hash invalidation, durable metadata, and Tool outcomes are unchanged.
- The production transcript is a selected conversation-block view. A user request begins a block, Agent text and concise Tool/permission activity stay attached to that block, the latest live Plan is shown only when Plan Mode is active, and trailing status is plain text with context usage and slash-command hints. No Turn labels, prototype controls, ANSI color, cursor positioning, or terminal width are required for meaning. Completion, failure, cancellation, and incomplete-stream states remain explicit.
- Provider conversation reconstruction admits only persisted `user.message`, `assistant.message` (including structured Tool Calls), and paired terminal `tool.completed`, `tool.failed`, or `tool.interrupted` messages. Proposed, validated, started, model, permission, configuration, Plan, manifest, compaction, recovery, lifecycle, and unknown events remain audit/projection data and never become synthetic user messages.
- A terminal Tool Result appears exactly once and retains its matching Tool Call pairing. Context Manifests record exact non-secret source identity, sequence or range, event type, and projection category for model-visible Session-derived messages; they never copy event payloads, prompts, Artifact bodies, or credentials. Summary, Plan, Recovery State, Tool Results, Artifacts, and repository text are data, and host authorization never depends on prompt wording.
- These remediation changes are compatibility-preserving: the existing Session Event schema and names, Provider-neutral message roles, Permission Policy semantics, exact Session grant key, persistence ordering, cancellation, interruption classification, compaction boundaries, and Resume invariants remain the source contract. Later remediation slices may change presentation and message selection only at their existing application seams.

### Tool contracts

- Every Tool has a stable name and description, Pydantic input model, side-effect category, cancellation/limit metadata, pure Risk Assessment, and structured Tool Result.
- `read_file` reads Workspace-relative UTF-8 text, at most 500 lines or 64 KiB, with an explicit continuation point.
- `search_files` searches literal or regex text with optional directory and glob, uses `rg` without a Shell when available, falls back to Python, and returns at most 200 matches or 64 KiB.
- `apply_patch` supports exact Add, Update, and Delete operations, at most 10 files and 256 KiB per call, with no fuzzy matching. Prepare changes before commit and use Checkpoints for ordinary rollback.
- `create_file` writes one UTF-8 file up to 256 KiB, may create parents, and refuses an existing target.
- `shell` uses PowerShell on Windows and a POSIX shell elsewhere, accepts a Workspace-relative working directory, filters credentials, rejects interactive/detached behavior, and bounds output and duration.
- File and search Tools default to 30 seconds; Shell defaults to 120 seconds and cannot exceed 10 minutes. Cancellation escalates through the platform's process-group controls.
- Built-in file Tools provide path confinement. Shell is not an operating-system sandbox and must be presented as such.

### Workspace and permissions

- Resolve one real Workspace root at startup. Model-facing file paths are relative; reject absolute, drive-changing, UNC, escaping, device, binary, and sensitive targets as applicable.
- Resolve existing links before read authorization; writes reject any link/reparse component and recheck the nearest real parent immediately before commit.
- Hard-deny `.mini-agent`, real environment-secret files, private keys, cloud credentials, credential stores, boundary escapes, and catastrophic deletion. Permit ordinary examples such as environment templates.
- Treat `AGENTS.md`, repository metadata, CI configuration, lockfiles, and security policy as Protected Paths whose writes always ask.
- Permission modes are suggest, auto-edit, and full-auto. Safe reads are automatic. Suggest asks for all writes and Shell. Auto-edit allows ordinary adds/updates but asks for Shell. Full-auto additionally allows only recognized local read/build/test Shell patterns. Deletes, Protected Path writes, network, install, Git writes, interpreters, redirection, chaining, and unknown Shell commands ask; hard hazards deny.
- Authorization order is schema validation, path normalization and sensitive checks, immutable Risk Assessment, hard deny, exact Session grant, mode default, optional confirmation, then final path and argument-hash recheck.
- Confirmation supports allow once and allow exact for Session. Any argument change creates a new Tool Call and decision.
- Persist a redacted Permission Decision with the Tool Call ID, risk, mode, decision, matched rule/reason, scope, normalized resource summary, argument hash, and timestamp.

### Tool lifecycle normalization

- Distinguish the business outcome carried by Tool Result from the Session Event name. Tool Result outcome is success, invalid, denied, failed, cancelled, or interrupted.
- Persist successful execution with `tool.completed`, known non-success outcomes with `tool.failed` and a typed outcome/category, and uncertain execution with `tool.interrupted`.
- This normalization reconciles the richer Tool Result outcomes with the compact event taxonomy; it does not add a second state machine.
- A persisted Tool Call has exactly one terminal Tool Result. Calls that never reached execution can still terminate as invalid or denied. A started operation whose termination or side effects cannot be proven is interrupted.

### Session Event model

- Store each Session under `.mini-agent/sessions/<session-id>/` with authoritative append-only `events.jsonl`, rebuildable metadata, immutable Artifacts, and Checkpoints.
- Each UTF-8 JSON line carries schema version, event ID, strictly increasing sequence, Session ID, optional Turn ID, type, timestamp, optional causation ID, and typed payload.
- Maintain one exclusive writer. Append complete lines and flush/fsync before treating a prerequisite event as durable.
- Event families cover Session lifecycle and instruction changes, user/assistant messages, model requests, Tool lifecycle, Plan snapshots, compaction, Artifacts, and Turn terminal states.
- `plan.updated` contains a full Plan snapshot. Each step has identity, description, pending/in-progress/completed status, optional result summary, and update time; at most one step is in progress.
- Metadata and projections are disposable. `events.jsonl` is the authority.
- Recovery may truncate only a trailing partial JSON line with a visible warning. Mid-file corruption or sequence gaps block automatic continuation.
- Unknown higher schema versions permit read-only inspection but block Resume and append. Old events migrate through pure in-memory transformations; permanent migration is explicit and backed up.

### Artifacts, redaction, and storage

- Tool output larger than the configurable 32 KiB persistence threshold becomes an immutable Artifact before its terminal event is appended.
- An Artifact reference includes identity, Session-relative path, media type, byte count, SHA-256, bounded preview, and truncation marker. Models receive the preview and use a controlled bounded reader for more.
- A Tool-specific 64 KiB response bound remains an absolute upper limit; the lower Artifact threshold determines when full content leaves inline context.
- Atomically write and verify Artifacts. An event failure may leave a discoverable orphan but cannot commit a successful Tool Result.
- Before persistence or model inclusion, redact known sensitive environment values and common credential formats. Retain no unredacted copy, and warn that detection is best-effort.
- Do not automatically expire Sessions. Explicit deletion is the only MVP retention action.

### Context assembly and compaction

- Context Frame authority layers are: non-overridable safety policy; core behavior and Permission Policy; structured Tool Definitions; effective `AGENTS.md`; Session summary/Plan/recovery state; selected typed post-boundary messages and paired terminal Tool Results; current user message.
- Later or lower-trust content cannot override higher safety. Ordinary repository content, Tool Results, Artifacts, and summaries never gain instruction authority.
- Load the Workspace-root `AGENTS.md`, then path-specific files from root toward each Tool target. Nearer files refine ordinary conventions. Conflicting multi-target rules block automatic execution. Never load instructions outside the Workspace.
- A single `AGENTS.md` defaults to 32 KiB and the chain to 128 KiB. Oversize or unreadable instructions warn and block relevant automatic work rather than being silently truncated.
- Derive a fresh Context Frame for every request. Preserve Tool Call/result pairing and use only typed message allowlists for provider content; keep unfinished lifecycle state available for audit, compaction, and recovery without rendering it as a synthetic message. Include previews instead of large bodies.
- Reserve response capacity before each request. Normally reserve the larger of 16,000 tokens or 20 percent of the context window, capped at 30 percent for small windows. Trigger compaction before exceeding the remainder.
- First micro-compact superseded state and large Tool Results. If insufficient, generate a validated structured Context Summary with objective, constraints, decisions, Plan, files, commands/results, failures, unresolved work, next actions, and references.
- A Summary Boundary records coverage. Active context is the latest valid summary plus relevant later events. Never delete original events or claim hidden reasoning was preserved.
- After three unsuccessful attempts to fit context, fail the Turn clearly.

### Configuration and prompts

- Ordinary precedence is built-in default, user TOML, project TOML, environment, CLI, then explicit Session override.
- Safety ceilings cannot be relaxed. Project configuration cannot set credentials or Provider Base URL. API Key comes only from `MINI_AGENT_API_KEY`; Base URL may come from user config, environment, or CLI.
- Strictly validate every existing TOML source; unknown keys and invalid types fail with source information. Effective Configuration is immutable and tracks each field's provenance and applied safety cap.
- Session overrides are allowlisted, recorded as non-secret events, and begin with the next operation. Less restrictive permission changes require explicit confirmation. `plan_mode` is runtime-only and may be changed only by an explicit one-shot or interactive action; it cannot come from TOML or environment sources. API Key, Base URL, Workspace, and Session storage cannot change in an active Session.
- Record a Context Manifest for each request with layer sources/hashes/token estimates, instruction hashes, configuration hash, non-secret request parameters, Summary Boundary, included event range, and exact provenance for model-visible Session-derived messages. Do not duplicate complete prompts, event payloads, Artifact bodies, or secrets.
- Keep the versioned core prompt small: Agent responsibility, Workspace, Loop completion, structured Tool use, permission obedience, honest verification, recovery, Plans, final reports, and no hidden chain-of-thought persistence. Enforceable host rules stay in code.

### Failure, retry, cancellation, and recovery

- Failure contains category, code, redacted message/details, source, retryability, required user action, and optional cause. Categories cover configuration, authentication, rate limit, network, Provider timeout/protocol, context overflow, permission denial, Tool validation/execution/timeout, persistence, cancellation, and internal error.
- Retry rate limits, transient network/connection failures, pre-stream timeout, and Provider 5xx at most twice after the initial request. Respect Retry-After or use jittered exponential backoff. Never retry a partially emitted response, persistence error, cancellation, or started side-effecting Tool automatically.
- Default Provider timeouts are 10 seconds connect, 60 seconds first event, 60 seconds stream idle, and 10 minutes total. Permission waits do not expire.
- First Ctrl+C acknowledges cancellation, stops new scheduling, and allows five seconds for cleanup. A second interrupt forces exit after best-effort recording. At idle, the first clears input and a consecutive interrupt exits.
- Persistence is a safety boundary: if `tool.started` cannot be durable, do not execute. If a terminal result, assistant message, or Turn terminal cannot be persisted, stop progression and never report durable success.
- Resume locks and validates the Session, repairs only an allowed partial tail, rebuilds projections, re-reads current instructions, and appends instruction-change notice when hashes differ.
- Reconcile interrupted reads by a new call; interrupted Patch through Checkpoint/current-hash comparison; interrupted Shell through command, working directory, output preview, and process evidence. Choices are inspect, abandon, retry as a new call, or exit. Never manufacture success.
- Use exit code 0 for normal completion/idle exit, 1 for runtime failure, 2 for configuration/usage error, and 130 for forced interruption. A failed Turn normally leaves the interactive CLI alive.

### CLI interaction

- Present the selected left-rail conversation-block transcript with user input, streamed Agent text, and concise Tool activity. Internal phase names and diagnostic action menus are not production UI. Supporting activity is attached to the originating Agent block and never rendered as a new user message.
- The supported command surface is the default interactive Agent command plus `init`, `sessions`, `resume`, `config show`, and `doctor`; `--workspace`, `--model`, `--permission-mode`, `--version`, and `--help` cover startup selection and diagnostics. In-session `/config` changes use the previously defined allowlist; `/plan on` and `/plan off` are explicit runtime controls for the next operation.
- Use transient status such as Thinking or Reading and collapse it after completion. The trailing status area may show context usage and slash-command hints; it scrolls with the transcript and has no pinned or cursor-dependent meaning.
- Display only the latest live Plan when Plan Mode is enabled and the complexity gate creates one. Clear it when complete, and never replay historical Plan snapshots as current UI.
- Interrupt streaming with a focused permission block only when a decision is needed. Show the four numeric choices and collapse the result into an audit line.
- Keep errors, instruction changes, uncertain side effects, and recovery choices prominent. Session listing is a dedicated command view.
- Non-interactive output is stable plain text without dynamic color. A required confirmation without interactive input denies safely.

### Diagnostics

- Correlate Session, Turn, request, Tool Call, and Failure IDs.
- Write redacted structured diagnostic JSONL under `.mini-agent/logs`, default INFO, rotating ten files of at most 10 MiB each.
- Never log API keys, authorization headers, full system prompts, or raw unredacted Tool output. Diagnostic-log failure does not hide the original Failure.
- Expose concise errors with an error ID and a doctor command that displays associated non-secret context. Never upload diagnostics automatically.

### Packaging and deployment

- Use `pyproject.toml`, Hatchling, uv, and a committed `uv.lock`. CI installs frozen development groups.
- Runtime dependencies are Typer, Rich, Pydantic, and httpx; development dependencies are isolated from installed runtime.
- Distribution name is `mini-agent`, import package is `mini_agent`, and console command is `mini-agent`; `python -m mini_agent` is equivalent.
- `pyproject.toml` is the sole version source. Start at 0.1.0 and use semantic versioning with a manually maintained changelog.
- Build a typed pure-Python wheel, source distribution, and SHA-256 checksums. Verify clean installation and Fake Provider smoke behavior on all supported platforms.
- Local wheel and Git installation satisfy MVP deployment. GitHub Release and PyPI publishing remain optional; public publishing uses the already tested artifacts and, if enabled, Trusted Publishing.
- Use the MIT License and state clearly that the project is independent, educational, and not Claude Code-compatible or Anthropic-endorsed.

### Implementation sequence

1. Establish package metadata, dependency lock, domain models, IDs, clock, and core port protocols.
2. Implement JSONL Session Store, event schemas, projections, exclusive locking, Artifacts, and deterministic recovery tests.
3. Implement Workspace path normalization, Tool contracts, bounded read/search, Patch Transaction/create, Shell process control, and Permission Gate.
4. Implement configuration loading, safety ceilings, `AGENTS.md` discovery, core prompt resources, Context Manifest, and Context Frame assembly.
5. Implement the OpenAI-compatible Provider Adapter, normalized Stream Events, protocol validation, timeout, retry, and Fake Provider parity.
6. Implement Agent Loop orchestration, Plan snapshots, Tool lifecycle normalization, cancellation, Failure mapping, and persistence-before-side-effect invariants.
7. Implement context estimation, micro-compaction, structured summary generation/validation, and Artifact rereading.
8. Implement conversational CLI, confirmations, Session list/Resume, doctor output, non-interactive fallback, and exit codes.
9. Integrate cross-platform process cleanup, interruption reconciliation, and all end-to-end Fake Provider journeys.
10. Build/install artifacts, run the complete CI matrix, then perform the optional manual real-model release-candidate checklist.

## Testing Decisions

### Test seams and philosophy

- Test observable domain and application behavior at the highest stable seam. Do not assert private method calls or broad ANSI snapshots.
- Use a scripted Fake Model Provider that emits text, Tool Calls, normal stops, partial streams, transient failures, illegal events, and usage while capturing each Context Frame. The shared CLI acceptance seam must run against a real temporary JSONL Session Store and expose rendered plain output, captured Context Frames, durable Session Events, and persisted Context Manifest metadata through public APIs rather than private-method assertions.
- Use an in-memory Session Store for exact transition tests and the real JSONL adapter in temporary directories for persistence, locking, tail repair, Artifact, and Resume integration tests.
- Use temporary real Workspaces for path and filesystem behavior, scripted User Interaction for confirmations/cancellation, and deterministic Clock/ID Generator values.
- Run shared contract suites against fakes and real adapters so tests cannot pass with a fake that violates production semantics.

### Automated layers

- Unit tests cover state transitions, message/Tool invariants, Permission Policy tables, Risk Assessment, configuration precedence, Context budgeting, Plan constraints, Failure mapping, and Session projections.
- Contract tests cover normalized Provider streams, Tool schemas/results, Session Events and migrations, Compactor schema/boundaries, and Application Port parity.
- Integration tests cover cross-platform paths and links, bounded Tools, Patch rollback, Shell cancellation, process trees, JSONL/fsync ordering, locks, corruption recovery, Artifacts, instruction discovery, compaction, and Resume reconciliation.
- A small CLI integration suite covers task entry, streaming, permission allow/deny, Plan update, cancellation, recoverable error, compaction, completion, Session list, and Resume using semantic assertions.
- The remediation acceptance baseline marks automatic Plan creation, word-based terminal confirmation, flat transcript formatting, and raw lifecycle-event-as-user-message reconstruction as superseded contracts. Their replacement checks are owned by the explicit Plan Mode, numeric confirmation, grouped transcript, and typed Context Frame slices; no test may preserve both production protocols as simultaneous expectations.
- Do not set an arbitrary coverage percentage. Every permission, confinement, durable-state, terminal-state, and interruption branch is mandatory.

### CI matrix

- Run Python 3.12 on Ubuntu, Windows, and macOS and Python 3.13 on Ubuntu.
- Run ruff lint/format checks, mypy, offline pytest, wheel/sdist build, clean wheel installation, `--help`, `--version`, and a Fake Provider smoke journey.
- Tests require no API Key and no real-model network calls. Any required platform job failure blocks merge.

### Acceptance matrix

| Capability | Required evidence |
| --- | --- |
| Installability | Clean wheel installs and exposes help/version and Fake Provider smoke flow on all supported OSes. |
| Agent Loop | Fake Provider drives text, serial Tool Calls, results, final response, bounded failure, and exact durable ordering. |
| Workspace safety | Absolute paths, traversal, external links, reparse points, sensitive targets, and protected writes follow the agreed allow/ask/deny rules. |
| Permission modes | Every operation class is tested in suggest, auto-edit, and full-auto, including exact grant invalidation after argument change. |
| Tool reliability | Read/search bounds, exact patching, rollback, create-no-overwrite, Shell timeout/output/process cleanup, and structured failures are observable. |
| Session durability | Contiguous JSONL, exclusive writer, partial-tail repair, mid-file refusal, projection rebuild, schema compatibility, and fsync-before-side-effect are tested. |
| Context lifecycle | Layer provenance, instruction scope, Tool pairing, Artifact threshold, compaction reserve, summary validation, and three-attempt terminal failure are tested. |
| Recovery | Forced exit after `tool.started` yields interrupted, never replay; inspect/abandon/new-call paths use actual Workspace evidence. |
| Streaming | Normal deltas render, illegal order fails, partial text stays incomplete, bounded backpressure preserves aggregate content, and no delta becomes a durable message. |
| Cancellation | First interrupt acknowledges and cleans up; uncertain Tools become interrupted; forced second interrupt exits 130; no new work is scheduled. |
| Failure handling | Retryable categories respect budget/backoff; non-retryable and persistence failures stop correctly; errors are redacted and correlated. |
| CLI UX | Production view omits diagnostic Phase/Actions, prompts only for real decisions, and reports completion, errors, compaction, and recovery clearly. |
| Conversational remediation | A real temporary-Session Fake Provider journey observes grouped plain output, explicit Plan Mode, numeric permission choices, typed Context Frames, durable events, and exact non-secret Context Manifest provenance; superseded automatic/word/flat/raw-event expectations are not treated as current behavior. |
| Configuration | Strict source validation, provenance, precedence, safety ceilings, environment-only key, and Session override lifecycle are tested. |
| Packaging | Wheel contents exclude scratch/runtime data/secrets, include typing/resources/license, and sdist rebuilds without Git metadata. |

### Manual real-model release-candidate checks

- In a temporary Git repository with a restricted credential, cover read-only exploration; confirmed edit and test; denied call and replanning; hazardous Shell confirmation; large Artifact; long-session compaction; forced exit and Resume; changed `AGENTS.md`; Provider rate limit/stream error/invalid Tool Call; and one complete read-modify-test-report journey per supported OS.
- Record date, Provider, model, Session ID, result, and failure notes without secrets. This is required for a release candidate, not for ordinary CI or MVP code review.

## Out of Scope

- Compatibility with Claude Code protocols, internal modules, prompts, Session formats, or unreleased behavior.
- Multiple production Model Provider implementations.
- Parallel Tool execution, sub-agents, MCP, Hooks, plugins, IDE integration, Web search, desktop UI, or Web UI.
- Container-grade sandboxing, virtual-machine isolation, cloud Session sync, branched Sessions, or cross-machine multi-writer storage.
- Interactive Shell programs, detached/background process guarantees, arbitrary model-controlled environment variables, or unrestricted full-auto authority.
- Image/binary editing, fuzzy patches, automatic Git commits, dependency installation by the Agent, or broad command catalogs.
- Hidden chain-of-thought storage, automatic long-term memory, prompt-cache optimizations, ML safety classifiers, or compatibility reverse engineering.
- Real-model calls in CI, model-quality benchmarking, or a guarantee that every coding task succeeds.
- Public PyPI publication, GitHub Release, standalone executables, native installers, Docker images, signing, SBOMs, automatic updates, or release-channel automation.

## Further Notes

- Public Claude Code information is architectural inspiration only. Reverse-engineered observations are clues, not contracts.
- The validated throwaway CLI prototype established the interaction state transitions, not the production visual design. The production CLI must remain conversational and context-sensitive.
- The Event Store is the central reliability boundary. If implementation pressure conflicts with durable-before-side-effect ordering, the implementation must stop rather than weaken that invariant.
- The specification intentionally favors explicit, testable host behavior over model prompting. A prompt instruction is never a substitute for path checks, Tool validation, permission enforcement, output limits, persistence, or cancellation.
- No in-scope design fog remains. Implementation may create ordinary engineering tickets, but any proposed expansion beyond this contract should be treated as a new product decision rather than silently added to the MVP.
