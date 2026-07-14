# Design context management and the session event model

Type: grilling
Status: resolved
Blocked by: 02

## Question

How should JSONL session events, active model context, large tool-result references, structured summaries, plans, resume behavior, and compaction thresholds fit together so an interrupted session can be resumed without pretending hidden reasoning was preserved?

## Answer

### Durable session layout and authority

Each session lives under `.mini-agent/sessions/<session-id>/` with `events.jsonl`, a rebuildable `metadata.json`, immutable large-result files under `artifacts/`, and reversible file snapshots under `checkpoints/`. `events.jsonl` is the sole authoritative append-only history. Metadata is only a cache for listing sessions; the current Plan and Context Summary are derived from events rather than separate authoritative files.

Each UTF-8 JSON line has `schema_version`, `event_id`, a strictly increasing session-local `sequence`, `session_id`, nullable `turn_id`, `type`, `timestamp`, nullable `causation_id`, and a typed `payload`. A session has one exclusive writer. A complete line is appended, flushed, and fsynced before a related irreversible side effect is treated as durably recorded. Recovery may truncate only a trailing partial JSON line and must surface a warning; corruption in the middle or a sequence gap blocks automatic continuation.

### Event vocabulary and projections

The MVP event vocabulary is:

- Session: `session.created`, `session.resumed`, `session.status_changed`, `session.instructions_changed`.
- Conversation: `message.user_added`, `message.assistant_added`.
- Model: `model.request_started`, `model.request_failed`.
- Tool lifecycle: `tool.proposed`, `tool.validated`, `tool.permission_decided`, `tool.started`, `tool.completed`, `tool.failed`, `tool.interrupted`.
- Plan: `plan.updated`.
- Compaction: `compaction.started`, `compaction.summary_created`, `compaction.failed`.
- Artifact: `artifact.created`.
- Turn: `turn.completed`, `turn.cancelled`, `turn.failed`.

Names describe facts that have happened, not commands. Events are never edited in place. Projections reconstruct current state in sequence order. Tool events share a `tool_call_id` so proposal, validation, authorization, execution, and terminal observation stay paired.

`plan.updated` contains a complete Plan snapshot, not a patch. Each step has `step_id`, `description`, `status` (`pending`, `in_progress`, or `completed`), nullable `result_summary`, and `updated_at`. At most one step is in progress. The latest valid snapshot is current, while previous snapshots preserve history. Simple Turns may omit a Plan.

### Artifacts and bounded tool results

Tool output larger than a configurable 32 KiB default is stored as an immutable Artifact. The file is written atomically before `artifact.created` and the terminal tool event are appended. References contain `artifact_id`, a session-relative path, media type, byte count, SHA-256 digest, a bounded preview, and `truncated: true`. Models receive the preview and reference by default and use a dedicated bounded read capability for more. The model cannot choose Artifact paths, paths cannot escape the session directory, and a failed Artifact write cannot produce a successful tool result.

### Active context derivation

Every model request derives a fresh Context Frame in this order: system prompt and tool definitions; currently effective `AGENTS.md` instructions; latest Plan; latest valid Context Summary; selected messages and necessary tool events after the summary boundary; and any unfinished tool lifecycle in the current Turn. Resume and normal continuation use the same assembler.

The assembler does not copy the full event log. It preserves provider-required tool-call/result pairing, includes Artifact previews rather than large bodies, and omits operational events such as locks unless they affect the task. The Context Frame is disposable; the event log remains authoritative.

### Compaction policy

Before a request, a conservative local estimator predicts input tokens. Provider-reported usage is recorded after requests to calibrate estimates. Normally:

```text
response_reserve = max(16_000 tokens, context_window * 20%)
compaction_limit = context_window - response_reserve
```

For small context windows, the reserve is capped so it consumes no more than 30% of the window. Configuration may override thresholds but cannot eliminate the minimum response reserve. Crossing the limit triggers compaction before the request.

Compaction is two-stage. Micro-compaction first replaces old large results with Artifact references and removes superseded, re-derivable state such as older Plan snapshots and intermediate permission presentation, while retaining user requirements, final assistant messages, file-change outcomes, errors, and unfinished tools. If that is insufficient, the model creates a structured Context Summary containing the objective, user constraints, confirmed decisions, current Plan, files read or changed, important commands and outcomes, failures or interruptions, unresolved questions, next actions, and Artifact references.

A summary records `covers_through_sequence`. The next Context Frame consists of that summary plus relevant events after the boundary. Recompaction may combine the prior summary with later events into a new summary, but no original event is deleted. The summary schema, boundary, and references are locally validated before it becomes active; a boundary cannot advance beyond persisted history or move backward. Invalid summaries produce `compaction.failed`. After three consecutive failed attempts to fit the request, the Turn fails clearly instead of sending a known-over-limit request.

Summaries preserve observable facts and working state only. They neither store nor claim to reconstruct hidden reasoning.

### Resume and instruction changes

Resume acquires the write lock, validates the log and supported schema, performs only permitted trailing-line repair, rebuilds projections, and identifies unfinished work. Any `tool.started` without a terminal event becomes `tool.interrupted`; it is never silently replayed, including when apparently idempotent. The user is shown the uncertain side effect, and a new model cycle may inspect, retry, or abandon it. After reconciliation is recorded, `session.resumed` is appended and the ordinary Context Frame assembler continues.

Current workspace instructions are re-read on Resume. Each request records their content hash. A changed hash creates `session.instructions_changed`, is disclosed to the user and model, and governs future actions without rewriting history. If it invalidates the current Plan, affected steps are reset or a replacement Plan is emitted. Only the hash and necessary change summary are logged, not a redundant full copy of potentially sensitive instructions.

### Concurrency, secrecy, and retention

Only one process may write a Session. The lock records PID, host identity, process start time, and acquisition time to guard against PID reuse. An active lock rejects another writer but does not prevent read-only listing or inspection. A lock is stale only when the recorded process is confirmed absent; takeover emits a recovery warning. Shared multi-machine session directories and multi-writer merging are outside the MVP.

Before persistence or model inclusion, tool output is best-effort redacted for common credential forms and known sensitive environment-variable values. Only the redacted form is retained. Shell execution receives no unrelated sensitive environment variables; permission events never include credential bodies; model file tools deny `.mini-agent/` by default, and Artifact bodies are exposed only through the controlled reader. The product must warn that scanning cannot guarantee detection of every secret. Sessions have no automatic expiry in the MVP and are deleted only by explicit user action.

### Schema evolution

Every event carries a schema version, and metadata caches the versions used at creation and last write. Readers support the current version plus explicitly supported older versions, projecting old events through pure in-memory migrations without rewriting the source log. A newer unknown version permits read-only inspection but blocks Resume and append. Unknown event types may be skipped only when declared observational and ignorable; anything that could affect permissions, plans, tool side effects, or terminal state blocks recovery. A future permanent upgrade must be an explicit, backed-up migration rather than an implicit startup mutation.
