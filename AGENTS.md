## Agent skills

### Issue tracker

Issues and PRDs are tracked in GitHub Issues for `teleno1/mini-agent`. See `docs/agents/issue-tracker.md`.

### Triage labels

Use the five default canonical triage labels. See `docs/agents/triage-labels.md`.

### Domain docs

This repository uses the single-context domain-documentation layout. See `docs/agents/domain.md`.

## Implementation source of truth

The implementation-ready specification is `docs/specs/mini-agent-mvp.md`. Implementation tickets, their blockers, assignments, acceptance evidence, and completion state live in GitHub Issues for `teleno1/mini-agent`.

The `.scratch/` directory contains historical or throwaway local material only. It is never an implementation-ticket tracker and its filenames or numbers must not be matched to GitHub Issue numbers.

When a ticket and the specification appear inconsistent, stop and surface the conflict. Do not silently change the ticket, weaken the specification, or choose whichever requirement is easier.

## GitHub implementation workflow

- When the user names a GitHub Issue URL or number, that Issue is the selected implementation ticket. Naming it authorizes the ticket's assignment, evidence comment, and closure; it does not authorize changes to other remote Issues.
- If the user does not name a ticket, select the lowest-numbered open `ready-for-agent` leaf GitHub Issue whose native blockers are all closed. A specification, map, or other parent Issue with unfinished sub-issues is not an implementation ticket and must be skipped.
- Work on one ticket at a time unless the user explicitly authorizes more.
- Before editing, read the complete GitHub Issue and comments, its relevant specification sections, the repository-root `CONTEXT.md`, and completed blocker Issues. Do not expect a ticket-local `CONTEXT.md` unless the selected Issue explicitly links one.
- Before editing, verify the blockers are closed and assign the selected GitHub Issue to the current GitHub user. This assignment is the claim.
- Treat every acceptance checkbox as required. Track each item in the work plan and verify it separately.
- Mark a checkbox `[x]` only when concrete evidence exists. Record that evidence in a GitHub Issue comment headed `## Completion evidence`.
- Close the selected GitHub Issue only after every acceptance checkbox is checked and evidenced. The closing comment must name the verification evidence and commit SHA. Otherwise leave it open and comment what remains.
- Do not implement later tickets or rewrite requirements to fit incomplete work.

## Git safety

- Do not push, create releases, or modify remote issues unless the user explicitly asks.
- Do not discard, overwrite, or commit unrelated changes belonging to the user or another session.
