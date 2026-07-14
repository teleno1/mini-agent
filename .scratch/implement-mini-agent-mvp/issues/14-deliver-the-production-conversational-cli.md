# 14 - Deliver the production conversational CLI

**What to build:** Users receive the validated production interaction: a compact conversational terminal that streams useful activity, asks only actionable questions, and makes Plans, failures, compaction, completion, Session management, and recovery clear without exposing the prototype's diagnostic dashboard.

**Blocked by:** 09 - Complete bounded serial multi-Tool coding Turns; 11 - Compact long Sessions without losing observable state; 12 - Cancel, retry, and diagnose failed Turns safely; 13 - Reconcile interrupted work on Resume.

**Status:** ready-for-agent

- [ ] Render user input, streamed Agent text, and concise Tool activity while hiding internal Phase, diagnostic Actions, and ordinary context percentages.
- [ ] Use transient status for thinking/reading, update Plans in place only for complex tasks, and collapse completed activity instead of accumulating noise.
- [ ] Show focused permission blocks with normalized operation, affected resources, reason, and only valid choices; collapse decisions into concise audit lines.
- [ ] Present incomplete output, retry progress, instruction changes, uncertain side effects, compaction, and recovery choices prominently and honestly.
- [ ] Implement dedicated `init`, `sessions`, `resume`, `config show`, and `doctor` views plus the agreed startup options and in-Session configuration commands.
- [ ] Completion reports outcome, verification, changed files, unresolved work, and next action; a failed Turn normally returns to interactive input.
- [ ] Non-interactive terminals use stable plain text, no dynamic color, and safe denial when confirmation input is unavailable.
- [ ] Semantic CLI integration tests cover task entry, text/tool streaming, allow/deny, Plan, compaction, cancellation, errors, completion, list, and Resume without broad ANSI snapshots.
