# Explicit Plan Mode research

Issue: [Define explicit Plan Mode without changing Session Event storage](https://github.com/teleno1/mini-agent/issues/4)

## Decision

Plan Mode should be a user-controlled Session setting, disabled by default. The existing complexity heuristic may remain as a second gate: enabling Plan Mode permits a Plan for complex work; it does not force a Plan for a simple text turn. No Plan may be created, updated, finalized, or rendered for a turn whose Plan Mode was disabled at turn start.

Use one setting named `plan_mode` with a boolean value. Expose it through the two existing interaction shapes:

- `--plan-mode` explicitly enables it for a one-shot invocation.
- Interactive `/plan on` and `/plan off` explicitly change the active Session setting for the next operation.

The setting must not be enabled implicitly by task complexity, model output, project instructions, environment variables, or ordinary user/project TOML. An explicit runtime action is required. The interactive change is persisted as a `configuration.changed` event using the existing `overrides` object; no new Session Event type or JSONL schema version is needed. A one-shot invocation should use the same Session override when it creates a Session so a later Resume does not lose the user's explicit choice.

Configuration changes take effect at the next operation, matching the existing Session-override contract. A running Turn keeps the mode captured at Turn start; toggling it cannot remove or rewrite an in-flight Plan.

## Current path and evidence

- `CONTEXT.md` defines Plan Mode as an explicit Session setting, disabled by default, and rejects automatic or implicit planning (`CONTEXT.md:147-150`).
- `AgentTurnApplication.run` currently creates a Plan after the first response containing Tool Calls when `_requires_plan(task, response.message.tool_calls)` is true (`src/mini_agent/application/agent.py:546-562`). `_requires_plan` returns true for multiple calls or task words spanning phases (`src/mini_agent/application/agent.py:1684-1700`).
- The same local Plan is advanced after Tool Results and finalized on a normal text response (`src/mini_agent/application/agent.py:503-511`, `src/mini_agent/application/agent.py:658-669`). Therefore one start-of-Turn gate suppresses the entire live Plan lifecycle.
- `plan.updated` stores a complete `PlanSnapshot`; the event type and payload already support the required durable history (`src/mini_agent/application/agent.py:1429-1444`, `docs/specs/mini-agent-mvp.md:117-126`).
- Projection appends every accepted snapshot to `plan_snapshots`. `plan.reset` only removes the current Turn's projected Plan and does not delete snapshots (`src/mini_agent/domain/sessions.py:378-397`). Existing Resume recovery can emit `plan.reset` for an interrupted Turn without changing old `plan.updated` events (`src/mini_agent/adapters/session_store.py:759-766`).
- Session overrides are already reconstructed from `configuration.changed` events and applied on the next operation (`src/mini_agent/domain/sessions.py:283-297`, `src/mini_agent/configuration.py:121-134`, `src/mini_agent/configuration.py:391-430`). The implementation should extend this allowlist with `plan_mode` while keeping it runtime-only.
- The CLI already has global options and an interactive command loop (`src/mini_agent/cli/app.py:839-877`, `src/mini_agent/cli/app.py:759-815`, `src/mini_agent/cli/app.py:906-910`). The Presenter is event-driven for `plan.updated` (`src/mini_agent/cli/presentation.py:140-141`), so suppressing the event path prevents Plan rendering without a second UI state machine.
- Existing tests assert automatic Plan creation for a complex multi-tool turn and no Plan for a simple text turn (`tests/test_ticket09.py:125-224`, `tests/test_ticket09.py:228-248`). The former expectation must become explicit opt-in coverage.

## Implementation contract

1. Add a validated boolean `plan_mode` value with default `false` to the effective runtime configuration or equivalent Turn input. Do not permit project/user TOML or environment sources to turn it on. Allow it only from an explicit CLI/runtime action and persisted Session override.
2. At Turn start, capture `plan_mode_enabled`. Change the automatic creation condition to `plan_mode_enabled and _requires_plan(...)`. Keep `_new_plan`, `_advance_plan`, `_finish_plan`, `plan.updated`, and `plan.reset` snapshot shapes unchanged.
3. Keep all existing Plan snapshot events readable on every Resume. A disabled mode suppresses new Plan events; it does not emit a compensating reset for a completed historical Plan and does not delete or migrate old snapshots.
4. Resume must restore the persisted setting through the existing `configuration.changed` projection. Existing interrupted-work handling remains authoritative: inspect, abandon, retry, and exit must not guess a Tool Result. If a resumed future Turn is disabled, it must not create a new Plan even when the old Session contains Plan snapshots.
5. The renderer should continue to render only live `plan.updated` lifecycle events. It must not render a historical `current_plan` merely because a resumed Session has one; historical Plan data remains available for inspection and context compaction.

## Acceptance evidence to add

- A complex multi-tool Turn with default settings produces no `plan.updated` events and no Plan output, while Tool execution and completion remain unchanged.
- The same Turn with explicit `--plan-mode` (or `/plan on`) produces the existing full snapshots and rendering, and a simple text Turn still produces none.
- `/plan off` applies on the next operation, persists a `configuration.changed` override, and leaves all earlier `plan.updated` snapshots readable.
- Resume after an interrupted Turn containing Plan snapshots preserves existing inspect/abandon/retry behavior and never replays or rewrites an old snapshot; a subsequent disabled Turn emits no new Plan event.
- The JSONL event types, `plan.updated` payload, `plan.reset` payload, and schema version remain unchanged.

## Scope note

The implementation-ready MVP specification currently describes Plans in terms of complex-task visibility and should be amended by the consolidation ticket to state the explicit Plan Mode gate. This is a narrow contract clarification, not a Session storage migration.
