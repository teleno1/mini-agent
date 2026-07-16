# 17 - Deny non-interactive permission prompts deterministically

**What to build:** A Coding Agent running without an interactive terminal fails closed whenever a Tool Call requires confirmation. Piped or redirected input cannot grant authority, while an interactive user retains the focused allow-once, allow-exact-for-Session, deny, and cancel choices.

**Blocked by:** None - can start immediately.

**Status:** ready-for-agent

- [ ] User Interaction receives or derives a trustworthy terminal-interactivity capability before presenting a permission decision.
- [ ] Every confirmation-required write, Protected Path operation, and Shell call is denied without prompting when terminal input is non-interactive.
- [ ] Piped affirmative values such as `allow` or `session` cannot authorize a Tool Call in non-interactive execution.
- [ ] The denial is persisted as a redacted Permission Decision linked to the exact Tool Call and the Agent Loop receives the corresponding denied Tool Result.
- [ ] Interactive terminals retain only the four valid focused choices and exact Session grants retain their existing argument-hash semantics.
- [ ] Semantic integration tests cover EOF, redirected/piped affirmative input, non-interactive one-shot execution, and interactive allow/deny behavior without broad ANSI snapshots.

## Completion evidence

Record concrete test names, persisted-event assertions, and observed terminal behavior here before marking this ticket completed.
