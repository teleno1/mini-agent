# Design the Agent Loop and runtime state machine

Type: grilling
Status: resolved
Blocked by: 01

## Question

What are the canonical runtime states, transitions, message types, tool-call lifecycle, completion conditions, cancellation semantics, and invariants for an asynchronous single-agent loop that supports streaming and remains testable with a Fake Model?

## Answer

### State model

Use two layers rather than one global enumeration:

- `SessionStatus`: `idle`, `running`, `waiting_for_user`, `completed`, `failed`, or `cancelled`. It is user-visible, durable, and reconstructable from Session Events.
- `TurnPhase`: `assembling_context`, `streaming_model`, `authorizing_tool`, `executing_tool`, or `recording_result`. It is transient and is not a resumable coroutine checkpoint.

A Turn may contain multiple model requests and Tool Calls. Recovery reconstructs durable Session state and reconciles unfinished work; it never resumes from an arbitrary Turn Phase.

### Messages and request context

The provider-neutral conversation model has three message types:

- `UserMessage` contains user input or follow-up instructions.
- `AssistantMessage` contains ordered `TextBlock` and `ToolCallBlock` values.
- `ToolResultMessage` references exactly one `tool_call_id` and reports success, recoverable failure, invalid input, denial, cancellation, or interruption.

System instructions, `AGENTS.md`, the Plan, and the latest Context Summary are assembled into a derived `ContextFrame`; they are not disguised as conversation messages. The Model Provider translates internal messages and Context Frames to its API format.

Streaming deltas are ephemeral UI events. A Tool Call's streamed arguments are buffered until complete, then parsed and validated. Persist only model-request start, a complete protocol-valid AssistantMessage, and request failure/cancellation—not individual deltas or partial messages.

### Tool Call lifecycle

The normal lifecycle is:

`proposed → validated → authorized → running → succeeded | failed | cancelled`

Early terminal paths are `proposed → invalid` and `validated → denied`. Every transition is a Session Event. Every persisted Tool Call eventually has exactly one Tool Result. Invalid, denied, and recoverable failed calls become ToolResultMessages so the model can adapt; host-level failures may fail the Turn.

When one AssistantMessage contains multiple Tool Calls, process them sequentially in model order and produce one result for each. Ordinary failure, invalid input, or denial does not suppress later calls. User cancellation or a host-level failure cancels all calls not yet started. Parallel read-only execution is deferred beyond the MVP.

On recovery, a call recorded as `running` without a terminal result becomes `interrupted`: its side effects are unknown, it is never automatically rerun, and the next model turn must inspect actual state before deciding what to do.

### Completion, cancellation, and retry

A Turn completes normally only when a complete AssistantMessage contains no Tool Calls and the Provider reports a normal stop. The final response must state verification performed or why verification was not possible; a Tool invocation is not mechanically required.

Limits on steps, elapsed time, or cost terminate as failure. Permission or clarification prompts transition to `waiting_for_user`. Provider protocol errors fail after their retry allowance is exhausted.

`Ctrl+C` cancels only the active Turn. During streaming it cancels the HTTP request and discards the partial AssistantMessage; during authorization it closes the prompt and cancels the proposed call; during Shell execution it terminates the process tree and escalates after a timeout. File operations must be short and atomic: an edit already committed is recorded as successful, not retroactively labelled cancelled. The Session returns to `idle`; `/exit` ends the interactive process.

Automatically retry only transient Provider failures that occur before a complete AssistantMessage exists and before any Tool executes. Use at most three attempts with jittered exponential backoff. Never stitch or replay a broken partial stream because duplicate Tool Calls cannot be ruled out. Context overflow is handled by compaction, not generic retry.

### Invariants

1. A Session has at most one active Turn.
2. A Turn has at most one active model request or executing Tool Call.
3. No Tool executes before validation and authorization.
4. Every persisted Tool Call has exactly one terminal Tool Result.
5. Required state is persisted before the next potentially side-effecting step.
6. Terminal or interrupted Tool Calls are never automatically rerun.
7. Provider objects, HTTP payloads, and UI state never enter domain events.
8. Step, time, cost, output, and retry limits make every loop bounded.

### Test seam

`ModelProvider` exposes an asynchronous internal event stream. A scripted Fake Model queues text, Tool Calls, transient errors, partial streams, and normal stops while capturing every ContextFrame it receives. Tool Registry, Permission Policy, Event Store, Clock, ID Generator, and user-confirmation UI are injected ports. Tests use deterministic time and IDs to assert exact event and transition order without network or terminal access.
