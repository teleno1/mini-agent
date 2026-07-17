# Issue tracker: GitHub

Issues and PRDs for this repo live as GitHub issues. Use the `gh` CLI for all operations.

## Conventions

- **Create an issue**: `gh issue create --title "..." --body "..."`
- **Read an issue**: `gh issue view <number> --comments`
- **List issues**: use `gh issue list` with suitable state and label filters.
- **Comment on an issue**: `gh issue comment <number> --body "..."`
- **Apply/remove labels**: use `gh issue edit` with `--add-label` or `--remove-label`.
- **Close an issue**: `gh issue close <number> --comment "..."`

## Implementation ticket lifecycle

GitHub Issues are the only implementation-ticket tracker. Do not infer a GitHub
Issue from a local filename or use `.scratch/` files as ticket state.

When an Agent is given a GitHub Issue URL or number, that Issue is the selected
ticket and the user has authorized the state changes for that Issue only:

1. Read the complete Issue and comments, relevant specification sections, the
   repository-root `CONTEXT.md`, and completed blocker Issues.
2. Verify native blockers are closed, then claim the Issue by assigning it to
   the current GitHub user **before editing**.
3. Implement and verify every acceptance criterion.
4. Post a `## Completion evidence` comment with concrete verification evidence
   and the commit SHA, then close the Issue. Leave it open when any criterion
   remains unmet.

Only the repository-root `CONTEXT.md` is required by default. Read an
additional context document only when the selected Issue explicitly links it.

Infer the repository from `git remote -v`; `gh` does this automatically inside the repository.

## Pull requests as a triage surface

**PRs as a request surface: no.**

GitHub issues are the sole request surface unless this flag is changed later.

## Skill operations

- When a skill says “publish to the issue tracker,” create a GitHub issue.
- When a skill says “fetch the relevant ticket,” run `gh issue view <number> --comments`.
- `/wayfinder` uses one labelled map issue with linked child issues.
- Prefer GitHub sub-issues and native issue dependencies where available.
- If unavailable, represent children with task lists and blockers with `Blocked by: #<n>`.
- Claim a ticket by assigning it to the current user.
- Resolve it by commenting with the answer and closing the issue.
