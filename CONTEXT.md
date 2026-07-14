# Domain Glossary

## Coding Agent

The interactive assistant that receives a coding task, reasons over repository context, invokes tools, observes their results, and continues until it can report an outcome.

## Workspace

The repository directory selected when Mini Agent starts. File operations are confined to this boundary unless the user explicitly grants broader access.

## Agent Loop

The repeated cycle in which the Coding Agent requests a model response, executes requested tools, returns observations to the model, and decides whether to continue or finish.

## Application Port

A narrow protocol owned by the application layer that represents an external capability needed to run a use case, allowing terminal, model, persistence, filesystem, time, and user-interaction adapters to be replaced without changing domain rules.

## Model Provider

The boundary through which the Agent Loop communicates with a language model. A provider translates Mini Agent messages and tool definitions to and from a model API.

## Stream Event

A normalized, ephemeral increment emitted by a Model Provider while a response is in progress. It may update the user interface, but only a successfully closed aggregate becomes a durable message or Tool Call.

## Failure

A stable, redacted description of an unsuccessful operation that identifies its category, source, retryability, required user action, and causal relationship without claiming an uncertain side effect did or did not occur.

## Context Frame

The complete input assembled for one model request from durable instructions, project instructions, the current Plan, the latest Context Summary, and selected conversation messages. It is derived input rather than a conversation message.

## Context Manifest

The non-secret provenance record for one Context Frame, identifying the sources, hashes, sizes, configuration, summary boundary, and event ranges used without duplicating their full content.

## Effective Configuration

The immutable, validated set of settings governing a Session at a given moment, with the winning source and any safety constraint retained for each field.

## Tool

A capability exposed to the model with a name, description, validated input, permission requirement, and observable result.

## Tool Call

A model-requested invocation of a named Tool with a unique identifier and proposed arguments. It has no authority to run until validation and the Permission Policy allow it.

## Tool Result

The structured observation produced by a Tool, containing either bounded success data or a recoverable error plus an optional reference to fuller persisted output.

## Interrupted Tool Call

A Tool Call that was recorded as running but had no result when its Session was recovered after an abnormal process exit. Its side effects are unknown, so it is never retried automatically and must be reconciled by inspecting actual state.

## Permission Policy

The selected rules that decide whether a proposed tool call runs automatically, requires user confirmation, or is rejected.

## Permission Decision

The auditable outcome that allows, asks about, or denies one immutable normalized Tool Call under a Permission Policy. A changed call requires a new decision.

## Risk Assessment

A Tool's structured description of a proposed call's side effects, affected resources, and hazards. It contains no authority; the Permission Policy uses it to allow, ask about, or deny the call.

## Protected Path

A Workspace path whose contents may be read when safe but whose modification always requires explicit user confirmation because it controls instructions, repository state, delivery, dependencies, or security policy.

## Session

The durable event history of one interaction between a user and the Coding Agent, including messages, tool calls, tool results, plans, and context summaries.

## Session Status

The durable, user-visible lifecycle condition of a Session, such as waiting, running, completed, failed, or cancelled. It can be reconstructed from Session Events.

## Turn

One response cycle initiated by user input and ending when the Coding Agent yields a final response, waits for the user, fails, or is cancelled. A Turn may contain multiple model requests and Tool calls.

## Turn Phase

The transient execution position within a Turn, such as assembling context, streaming a model response, authorizing a Tool, executing it, or recording its result. It is not a resumable checkpoint.

## Session Event

An append-only fact recorded during a Session, such as a message, permission decision, tool lifecycle change, context compaction, cancellation, or terminal error. Current session state is derived from these events.

## Session Projection

A disposable current-state view reconstructed in event-sequence order from a Session's authoritative event history, such as its status, current Plan, or latest valid Context Summary.

## Artifact

An immutable, session-local file containing a large Tool Result that is represented in Session Events and model context by a bounded preview plus an integrity-checked reference.

## Summary Boundary

The event sequence through which a validated Context Summary claims coverage. Active context combines the latest valid summary with relevant events after this boundary.

## Checkpoint

A session-local record that can reverse file changes made through Mini Agent. It does not reverse shell commands or external side effects and is not a substitute for Git.

## Patch Transaction

One validated group of text-file changes intended to succeed or be rolled back as a unit. Multi-file commits are not crash-atomic; an abnormal exit can leave an Interrupted Tool Call whose actual file state must be reconciled.

## Context Summary

A structured replacement for older session events when the active model context approaches its limit. It preserves decisions, progress, relevant facts, and unresolved work without claiming to preserve hidden reasoning.

## Plan

The user-visible, mutable list of task steps and their states maintained during a Session.
