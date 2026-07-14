# Issue tracker: GitHub

Issues and PRDs for this repo live as GitHub issues. Use the `gh` CLI for all operations.

## Conventions

- **Create an issue**: `gh issue create --title "..." --body "..."`
- **Read an issue**: `gh issue view <number> --comments`
- **List issues**: use `gh issue list` with suitable state and label filters.
- **Comment on an issue**: `gh issue comment <number> --body "..."`
- **Apply/remove labels**: use `gh issue edit` with `--add-label` or `--remove-label`.
- **Close an issue**: `gh issue close <number> --comment "..."`

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
