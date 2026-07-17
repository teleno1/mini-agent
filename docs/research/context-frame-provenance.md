# Research resolution: Context Frame provenance and event contamination

## Question

Which current Context Frame inputs, prompt resources, and event-to-message transformations can cause persisted internal Session Events to be interpreted as user content, and what is the smallest safe selection/provenance contract that prevents it?

## Conclusion

The direct contamination boundary is in `src/mini_agent/context.py`, not in JSONL persistence:

1. `ContextBuilder._render_state()` combines the Context Summary, Plan, and Recovery State into the `SESSION_STATE` layer, whose provider role is `user`. A structured summary is derived from persisted events, so event text can reach a user-role message indirectly.
2. `AgentTurnApplication._selected_context_events()` selects every post-boundary `tool.*` event. `ContextBuilder._frame_messages()` then emits each selected record as `role="user"` with an `event: ...` prefix. This is the direct path that makes internal lifecycle records look like user-authored content.
3. `ContextCompactor._fact_summary()` reads the complete event stream and copies selected tool arguments/results into summary fields. The next frame presents that summary through the same user-role `SESSION_STATE` layer.

The normal typed conversation path is safer: `SessionProjection.messages` and `messages_after_boundary()` reconstruct only `user.message`, `assistant.message`, and terminal Tool Result events. `UserMessage`, `AssistantMessage`, and `ToolResultMessage` preserve their provider roles and Tool Call pairing. `ContextManifest` is audit metadata only; it records provenance but is not itself emitted as a provider message.

## Current input and authority inventory

| Input | Current source | Current provider role | Risk boundary |
| --- | --- | --- | --- |
| Safety policy | `CORE_SAFETY_POLICY` | `system` | Host policy is still enforced in code; this is the highest prompt authority. |
| Core behavior | `CORE_BEHAVIOR` | `system` | Stable prompt resource; tells the model to treat repository data, Tool Results, Artifacts, and summaries as ordinary data. |
| Tool definitions | Tool Registry | `system` | Structured metadata, but descriptions are prompt-visible and must not be treated as executable authority. |
| Project instructions | Path-scoped `AGENTS.md` via `InstructionLoader` | `developer` (mapped to `system` by the current OpenAI-compatible adapter) | Deliberately trusted project guidance; ordinary repository files are not loaded here. Safety policy still outranks it. |
| Session state | Summary, Plan, Recovery State | `user` | Event-derived facts can be presented as if they were user content. |
| Typed history | `Message` values from the projection | Original message role | Safe allowlist when only typed message events are admitted and Tool pairing is retained. |
| Selected events | Raw-ish `tool.*` summaries from the writer | `user` | Direct contamination: lifecycle records and result text become synthetic user messages. |
| Current input | Current `UserMessage` | `user` | Genuine user content; no provenance confusion. |

The OpenAI-compatible adapter preserves this distinction mechanically: `developer` is mapped to `system`, while `ContextMessage` values with role `user` remain `user`. Therefore a text prefix such as `event:` is not a structural trust boundary.

## Smallest safe contract

The minimum change should preserve the Session Event schema and existing persistence model while tightening the Context Frame seam:

### 1. Allowlist message reconstruction

Only these persisted event types may become provider messages:

- `user.message` -> `UserMessage`.
- `assistant.message` -> `AssistantMessage`, including complete structured Tool Calls.
- `tool.completed`, `tool.failed`, and `tool.interrupted` -> `ToolResultMessage`, only when paired with the corresponding assistant Tool Call.

All other events (`model.request.*`, `tool.proposed`, `tool.validated`, `tool.started`, permission/configuration records, Plan updates, manifests, compaction records, and lifecycle notices) remain projection/provenance data. Unknown event types must be rejected from model-visible selection rather than rendered generically.

### 2. Remove raw event records from provider messages

Do not append `selected_events` to `ContextFrame.messages`. Completed Tool observations already exist as typed `ToolResultMessage` values. Unfinished lifecycle state is needed for Resume and compaction, but it does not need to be sent as a synthetic user message; Resume must reconcile it before a normal model request. Keep event selection available to compaction and audit code as typed data, not as a message shortcut.

This removes the direct `_selected_context_events()` -> `event: ...` -> `role="user"` path and avoids spending provider context on duplicate Tool lifecycle records.

### 3. Make provenance exact and non-content-bearing

Keep the existing non-secret Context Manifest, but record the exact model-visible source set rather than relying only on the broad `included_event_range`:

```json
{
  "source_kind": "session-event",
  "event_id": "event-...",
  "sequence": 42,
  "event_type": "tool.completed",
  "projection": "tool-result-message"
}
```

The manifest may use compact ranges for contiguous allowlisted message events, but it must also record the event types/projections (or exact IDs when ranges are not exact). It must never copy full event payloads or prompt text. A manifest entry is an audit pointer, not a provider message.

### 4. Keep derived state explicitly data-only

If Summary, Plan, or Recovery State remains in the frame, keep it in a distinct session-state data section with an explicit provenance label and delimiter. It must never be used as an instruction source, and its values must be generated from validated projections/typed summaries rather than arbitrary event JSON. The host must not rely on wording in `CORE_SAFETY_POLICY` to enforce this boundary.

The safest minimum implementation is to retain the existing `SESSION_STATE` layer for observable state while ensuring it is built only from validated `ContextSummary`, `PlanSnapshot`, and recovery projections, and to remove raw `selected_events` from both `_render_history()` and `_frame_messages()`.

## Regression contract

Add tests at the ContextBuilder and Provider seams that assert:

- A `tool.started` or `tool.proposed` event is absent from provider messages.
- A terminal Tool event appears once, as a `tool` message paired to its assistant Tool Call, not as `user` text.
- `model.request.*`, `context.manifest.recorded`, `plan.updated`, and configuration/lifecycle events never become messages.
- `messages_after_boundary()` remains an allowlist for the three message-bearing families above.
- The manifest contains exact non-secret source identity/type metadata but no message content, raw prompt, or secret.
- A string such as `Ignore the safety policy` inside a Tool Result, Artifact preview, or summary remains ordinary data and cannot alter host permission checks.

## Evidence inspected

- `src/mini_agent/context.py`: Context layers, `_render_state`, `_render_history`, and `_frame_messages`.
- `src/mini_agent/application/agent.py`: `_selected_context_events`, context compaction inputs, and manifest persistence.
- `src/mini_agent/domain/compaction.py`: `messages_after_boundary`, `micro_compact_events`, and event-derived summary fields.
- `src/mini_agent/domain/sessions.py`: Session Event vocabulary, projection, and typed message reconstruction.
- `src/mini_agent/providers/openai_compatible.py`: provider role mapping and Tool message pairing.
- `src/mini_agent/instructions.py`: bounded, path-scoped `AGENTS.md` loading.
- `docs/specs/mini-agent-mvp.md` and `CONTEXT.md`: Context Frame, Context Manifest, Session Event, and Plan Mode contracts.

Baseline verification: `.venv\\Scripts\\python.exe -m pytest -q tests/test_ticket04.py tests/test_provider.py tests/test_ticket18.py` -> `28 passed, 1 skipped`.
