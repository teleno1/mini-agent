# Numeric Permission-Confirmation Contract

Status: resolved research for [Specify the numeric permission-confirmation contract](https://github.com/teleno1/mini-agent/issues/5)

## Decision

Replace the production terminal's word-based permission prompt with one focused,
numeric menu. Keep the existing semantic `ConfirmationChoice` values and
`PermissionPolicyGate`; the terminal adapter is the only layer that translates
human input.

The menu is:

| Input | Semantic result | Effect |
| --- | --- | --- |
| `1` | `ALLOW_ONCE` | Authorize this exact Tool Call once. |
| `2` | `ALLOW_FOR_SESSION` | Authorize this exact Tool, normalized resource set, and argument hash for the current Session. |
| `3` | `DENY` | Do not execute the Tool Call; return a denied Tool Result so the Agent can observe the denial. |
| `4` | `CANCEL` | Cancel the pending Tool Call/Turn according to the existing cancellation path. |

The user-facing prompt should be stable plain text and show both the number and
meaning, for example:

```text
  Choose [1 allow once / 2 allow exact for Session / 3 deny / 4 cancel]:
```

## Input contract

- Parse one trimmed ASCII digit from the set `1`, `2`, `3`, `4`.
- Empty input, multi-digit input, punctuation, booleans, and words are invalid.
- In an interactive terminal, an invalid value emits one concise error and
  re-prompts. It does not create a Permission Decision, Session grant, or Tool
  lifecycle event.
- `EOF`, `Abort`, and terminal I/O failure retain the current fail-closed
  behavior: return `DENY` without executing the Tool.
- The numeric parser belongs in `TerminalPermissionInteraction`, not in the
  domain Permission Policy. The policy continues to receive semantic choices.

## Compatibility boundary

`ConfirmationChoice` remains the stable programmatic vocabulary, with its
existing values (`allow-once`, `allow-exact-for-session`, `deny`, and `cancel`).
Injected test or application interactions may continue returning those enum
members, and the existing policy normalization may continue accepting those
semantic strings for this non-terminal seam. The production CLI does not accept
word aliases such as `allow`, `session`, or `yes`; this avoids two user-facing
protocols and makes the numeric contract testable.

No change is needed to `PermissionDecision`, the exact Session-grant key, the
argument-hash invalidation rule, Permission Decision metadata, Session Event
storage, or non-interactive detection.

## Failure and non-interactive behavior

The permission gate must still perform hard-deny checks, exact-grant lookup, and
mode defaults before asking for input. A numeric answer cannot authorize a
hard-denied hazard, and a grant for one argument hash cannot authorize a changed
Tool Call.

When `is_interactive` is false, the terminal interaction must not render the
permission menu or call `typer.prompt`. The gate records the existing
`non-interactive-input` rule and returns `DENY`; the application may persist the
denied Tool Result and continue the Agent Loop. Piped text, including `1`, must
not be treated as consent.

## Required implementation evidence

1. Unit tests prove the four numeric inputs map exactly to the four semantic
   choices, including `4` producing `PermissionDecision.CANCEL`.
2. Interactive tests prove invalid numeric/text input re-prompts, and that
   aliases are not accepted by the terminal adapter.
3. Existing exact-session-grant and changed-argument-hash tests remain green;
   numeric input must not alter grant scope.
4. Non-interactive tests prove no prompt/menu is emitted, no `tool.started`
   event is appended, and the durable Permission Decision uses
   `non-interactive-input` with a denied Tool Result.
5. A CLI integration test checks the stable menu/error text in semantic plain
   output without asserting ANSI or cursor behavior.

## Sources

- [PermissionChoice, preview, confirmation seam, and policy behavior](../../src/mini_agent/application/permissions.py#L19-L254)
- [Provider-neutral PermissionDecision contract](../../src/mini_agent/tools/contracts.py#L39-L45)
- [Current terminal prompt and fail-closed exception path](../../src/mini_agent/cli/presentation.py#L287-L323)
- [Terminal capability detection](../../src/mini_agent/cli/app.py#L119-L124)
- [MVP permission, exact-grant, persistence, and non-interactive requirements](../specs/mini-agent-mvp.md#L99-L108)
- [MVP non-interactive output requirement](../specs/mini-agent-mvp.md#L172-L176)
- [Existing non-interactive and four-choice tests](../../tests/test_ticket17.py#L103-L188)
