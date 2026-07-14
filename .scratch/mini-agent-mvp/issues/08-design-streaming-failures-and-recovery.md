# Design streaming, failure classification, and recovery

Type: grilling
Status: resolved
Blocked by: 03, 04, 05

## Question

What normalized Provider/UI streaming events, error taxonomy, retry budgets, timeout rules, cancellation acknowledgements, interrupted-operation reconciliation steps, and user-visible recovery choices should apply across model, Tool, Event Store, and terminal failures while preserving the Agent Loop invariants?

## Answer

### Normalized streaming protocol

Provider Adapters emit `response.started`, `text.delta`, `tool_call.started`, `tool_call.arguments_delta`, `tool_call.completed`, `usage.updated`, `response.completed`, and `response.failed`. Every event carries a `request_id`; Tool Call events carry a stable `tool_call_id`. Unknown events and illegal orderings are Provider protocol failures rather than input to guess at.

Text and argument deltas are ephemeral UI data and are not individually persisted. A complete assistant message is appended only after successful response closure; a Tool Call becomes durable only after its arguments close and parse. Tool Calls are executed serially in arrival order. A stream that ends before `response.completed` cannot create a final assistant message.

On an interrupted stream, retain the already rendered text visibly as `incomplete`, append `model.request_failed` with the redacted category, retryability, received character count, short preview, and unfinished Tool IDs, and discard partial Tool arguments. A subsequent request begins from the last durable context rather than feeding the incomplete text back as history.

### Failure model

Every boundary maps errors into a stable Failure carrying `category`, `code`, redacted `message`, `source`, `retryable`, `user_action_required`, redacted `details`, and optional `caused_by`. Categories are `configuration`, `authentication`, `rate_limit`, `network`, `provider_timeout`, `provider_protocol`, `context_overflow`, `permission_denied`, `tool_validation`, `tool_execution`, `tool_timeout`, `persistence`, `cancelled`, and `internal`.

Provider exceptions are translated at the adapter boundary. User messages state what failed, whether retry is safe, and whether side effects may exist. Internal failures have a correlation ID and local diagnostic record rather than being swallowed.

### Retry and timeout policy

Rate limits, transient networking, connection failure, pre-stream timeout, and Provider 5xx allow at most three total attempts: the initial request plus two retries. Respect `Retry-After`; otherwise use jittered exponential delays of roughly one then two seconds, capped at 30 seconds. Retries are visible and cancellable.

Do not automatically retry authentication, configuration, protocol errors, a stream that has already emitted content, persistence failure, cancellation, or any started Tool with possible side effects. Context overflow uses the separate three-attempt compaction budget. Tool failures, including read-only Tools, return to the Agent for an explicit new call rather than retrying inside the executor. Each attempt receives a new request ID linked by causation.

Default model boundaries are 10 seconds to connect, 60 seconds to the first event, 60 seconds of stream inactivity, and 10 minutes total. File, search, and Patch Tools default to 30 seconds; Shell defaults to 120 seconds. A timed-out Tool gets a cooperative cancellation and five-second cleanup period before controlled force termination. Project configuration cannot enlarge user safety caps. Permission waits do not time out. Every timeout records the specific boundary and elapsed time.

### Cancellation and process cleanup

The first Ctrl+C stops new event acceptance and Tool scheduling, acknowledges `Cancelling...` within one second, requests cooperative cancellation, and waits up to five seconds. A clean stop appends `turn.cancelled` and returns to Session input. An operation whose termination cannot be proven becomes interrupted. A second Ctrl+C during cleanup forces process exit after best-effort persistence; Resume performs normal reconciliation.

At idle input, Ctrl+C clears input and a consecutive Ctrl+C exits. During permission confirmation it cancels the Turn rather than merely denying one call. EOF exits at idle and follows cancellation semantics during active work. Cancellation is terminal and never retried.

Shell Tools run in an isolated process group. Unix escalation is SIGINT, SIGTERM, then SIGKILL; Windows uses a new process group, control interruption, then controlled process-tree termination. Output-reader tasks share the process lifecycle. Only confirmed termination yields cancelled or failed; uncertainty yields interrupted. In-process Tools use cooperative asyncio cancellation. Patch cancellation attempts rollback, with failed rollback producing an interrupted result and the possibly affected paths. Detached or external processes cannot be guaranteed; permission rules should prevent their automatic execution.

### Persistence as a safety boundary

No durable record means no next side effect. If `tool.started` cannot be appended and fsynced, the Tool does not run. If a Tool finishes but its terminal event cannot be persisted, stop the Agent Loop and reconcile it as interrupted on Resume. An assistant message or Turn whose terminal event cannot be persisted remains visibly unsaved and is never reported as durably complete.

Artifacts are atomically written and verified before reference events; an event failure may leave a discoverable orphan Artifact but cannot commit a result. Persistence failures are not blindly retried because duplicate appends create ambiguity. If even the failure cannot be recorded, show a direct warning and exit nonzero.

### Interrupted Tool reconciliation

Resume presents evidence rather than replaying. Read/search may be proposed again under a new Tool Call ID. Patch reconciliation compares its Checkpoint, current hashes, and expected change, then either observes a fully applied state or offers rollback/new patch for partial application. Shell reconciliation shows the redacted command, working directory, start time, captured preview, and whether the known process remains.

Common choices are `inspect`, `abandon`, `retry as new call`, and `exit`. A retry repeats validation and permission under a new ID. The user cannot mark the original call successful, because that would manufacture a Tool Result without reliable observation.

### UI backpressure and terminal failures

Use a bounded queue between Provider reading and rendering. Coalesce text refreshes at roughly 30–60 ms without changing aggregate content; do not render partial Tool arguments. When full, the queue applies upstream backpressure rather than dropping content. Large output follows Artifact limits.

A closed stdout or broken pipe cancels the Turn and exits nonzero after best-effort recording. Rich/ANSI failure falls back to plain text without damaging the Agent Loop. Non-interactive output disables dynamic refresh and color; a confirmation that cannot receive input is safely denied rather than hanging.

Recoverable Provider failures expose retry, edit request, and cancel Turn. Authentication/configuration failures explain remediation without spending retry budget. Protocol and internal failures end only the Turn and preserve the Session. Ordinary Tool validation/execution failures return as structured observations unless user choice or uncertain side effects require interruption. Permission denial is a normal result. Persistence failure stops execution.

Process exit codes are 0 for normal completion or idle user exit, 1 for runtime failure, 2 for configuration/usage error, and 130 for forced user interruption. A failed Turn usually does not terminate interactive mode, and Provider HTTP status codes never become platform-dependent process exit codes.

### Diagnostics and invariants

Session, Turn, request, Tool Call, and Failure IDs provide correlation. Redacted structured JSONL diagnostics live under `.mini-agent/logs/`, default to INFO, rotate at ten 10 MiB files, and never include credentials, authorization headers, full system prompts, or raw unredacted Tool output. The CLI shows concise errors plus an `error_id`; `mini-agent doctor --error <id>` presents associated non-secret details. Logs are never uploaded automatically and their failure does not obscure the original error.

Each request, Tool Call, and Turn has at most one durable terminal state; completed, failed, cancelled, and interrupted are mutually exclusive. No Tool runs before complete validated arguments. Cancellation schedules no new work. Retries use new IDs. Started side effects are never automatically retried. Event Store failure halts progression. UI output is not automatically durable fact. Unknown outcomes are interrupted, and Resume continues only from durable events plus newly observed reality.
