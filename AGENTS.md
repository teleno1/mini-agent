## Agent skills

### Issue tracker

Issues and PRDs are tracked in GitHub Issues for `teleno1/mini-agent`. See `docs/agents/issue-tracker.md`.

### Triage labels

Use the five default canonical triage labels. See `docs/agents/triage-labels.md`.

### Domain docs

This repository uses the single-context domain-documentation layout. See `docs/agents/domain.md`.

## Implementation source of truth

The implementation-ready specification is `docs/specs/mini-agent-mvp.md`. Implementation tickets live in `.scratch/implement-mini-agent-mvp/issues/`.

When a ticket and the specification appear inconsistent, stop and surface the conflict. Do not silently change the ticket, weaken the specification, or choose whichever requirement is easier.

## Ticket-specific rules

- If the user does not name a ticket, select the lowest-numbered `ready-for-agent` ticket whose blockers are all `completed`.
- Work on one ticket at a time unless the user explicitly authorizes more.
- Before editing, read the complete ticket, its relevant specification sections, `CONTEXT.md`, and completed blocker tickets.
- Set the selected ticket to `in-progress` when work begins.
- Treat every acceptance checkbox as required. Track each item in the work plan and verify it separately.
- Mark a checkbox `[x]` only when concrete evidence exists. Record that evidence under `## Completion evidence` in the ticket.
- Set the ticket to `completed` only after every acceptance checkbox is checked and evidenced. Otherwise leave it `in-progress` and record what remains.
- Do not implement later tickets or rewrite requirements to fit incomplete work.

## Git safety

- Do not push, create releases, or modify remote issues unless the user explicitly asks.
- Do not discard, overwrite, or commit unrelated changes belonging to the user or another session.
