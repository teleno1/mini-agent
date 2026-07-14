# Prototype the CLI interaction and confirmation flow

Type: prototype
Status: resolved
Blocked by: none

## Question

What should users see and do during task entry, streamed model output, tool-call previews, permission confirmations, plan updates, interruption, compaction, errors, completion, session listing, and session resume?

Create only a cheap interaction prototype for human feedback; do not implement production runtime behavior.

## Answer

The interaction state machine and its transitions were validated with the throwaway terminal prototype at [`../../prototypes/cli-interaction/`](../../prototypes/cli-interaction/). The prototype covers task entry, streamed chunks, Plan changes, tool previews, allow/deny decisions, context compaction and failure, provider errors and retry, interruption, completion, Session listing, and Resume. Human review found no problem with the overall flow or state transitions.

The prototype's persistent `Phase`, `Status`, `Actions`, and context-percentage fields are diagnostic instrumentation required to expose its complete state; they are not the production presentation.

### Production-facing interaction

- The default screen is a conversational transcript: user input, streamed Agent text, and concise tool activity.
- Internal phase names are never shown.
- The normal interface does not present an action-command menu. The user types natural language, while context-specific prompts expose choices only when an explicit decision is required.
- Transient work appears as a short status line such as `Thinking...` or `Reading src/parser.py...`; it is replaced or collapsed when the operation finishes rather than accumulating as noise.
- Context utilization stays hidden during ordinary work. The CLI reports it only when approaching the compaction threshold, while compacting, or when compaction cannot recover enough space.
- A Plan is visible for complex tasks, updates in place, and remains absent for simple Turns.
- A Tool Call that needs permission interrupts streaming with a focused confirmation block showing the normalized operation, affected resources, risk reason, and the choices allowed by the current Permission Policy. After the decision it collapses into a concise audit line.
- Errors, interruption recovery, instruction changes, and uncertain Tool side effects remain prominent because the user must understand or act on them.
- Completion shows the outcome, verification performed, changed files, unresolved items, and any suggested next action.
- Session listing is a dedicated command view rather than part of the ordinary conversation. Resume identifies the selected Session and surfaces only actionable recovery facts before returning to the conversational view.

### Validated transitions

The user can enter a task, observe streaming, inspect Plan progress, approve or deny a proposed Tool Call, interrupt at any interactive point, retry recoverable provider failures, observe compaction without losing the Session, complete a Turn, list Sessions, and Resume an interrupted Session. Permission prompts accept only their displayed decisions; unavailable diagnostic actions from the prototype do not become production commands.

The production CLI may reuse the validated interaction states, but the throwaway renderer must not be shipped as the real interface.
